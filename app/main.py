"""FastAPI entrypoint for the public Render deployment.

Routes
  GET  /                 the single-page UI
  GET  /health           liveness + configuration summary (no secrets)
  GET  /config           only non-secret defaults + booleans
  POST /plan             dry-run: what will this date range cost?
  POST /search           start a scan -> {job_id} immediately
  GET  /search/{job_id}  progress + results (polled by the browser)
  POST /search/{job_id}/cancel
  GET  /ui/config        (same as /config, kept for older front-ends)

Credential policy (enforced, not advisory)
  * Every scan requires the visitor's OWN LLM key. Keys are used per request,
    never stored, never logged, never written to disk.
  * A server key is only reachable when ALLOW_SERVER_LLM_FALLBACK=true, and
    even then it is never sent to the browser.
"""
from __future__ import annotations

import logging
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.config import settings
from app.headers import SecurityHeadersMiddleware
from app.jobs import describe_plan, run_scan, store
from app.llm_adapter import LLMError, fetch_models
from app.security import (
    ConcurrencyGate,
    RateLimiter,
    ValidationError,
    validate_api_key,
    validate_channel,
    validate_criteria,
    validate_llm_base_url,
    validate_model,
)
from app.window import WindowError

logging.basicConfig(
    level=getattr(logging, settings.log_level, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("job_filter")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "..", "static")

app = FastAPI(title="Job Radar", version="2.0.0")
app.add_middleware(SecurityHeadersMiddleware)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

limiter = RateLimiter(
    per_hour=settings.rate_limit_scans_per_hour,
    per_ip_concurrency=settings.rate_limit_ip_concurrency,
)
gate = ConcurrencyGate(settings.global_concurrency)

# Model listing is cheap but calls an external API: give it its own generous
# bucket so it never eats into the scan quota.
_model_probe = RateLimiter(per_hour=60, per_ip_concurrency=2)

STARTED_AT = time.time()


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class PlanRequest(BaseModel):
    since: str
    tz_offset_minutes: int = 0


class ModelsRequest(BaseModel):
    llm_base_url: str
    llm_api_key: str


class SearchRequest(BaseModel):
    channel: str
    criteria: str
    since: str
    tz_offset_minutes: int = 0
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""


def client_ip(request: Request) -> str:
    """Best-effort client identity for rate limiting.

    Render terminates TLS and appends the real peer to X-Forwarded-For, so the
    *right-most* entry is the one Render wrote; everything to its left is
    client-supplied and can be spoofed. Using [0] (the old code) let anyone
    dodge the per-IP quota by inventing a new header on every request.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            return parts[-1]
    return request.client.host if request.client else "unknown"


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
async def health():
    """Render health check. Must be cheap and must never fail on missing config."""
    info = settings.describe()
    info.update(
        {
            "status": "ok",
            "uptime_seconds": int(time.time() - STARTED_AT),
            "active_scans": store.active_count,
            "global_concurrency_limit": settings.global_concurrency,
        }
    )
    return info


@app.get("/config")
async def get_config():
    """Non-secret configuration for the UI.

    Deliberately does NOT return LLM_API_KEY. The old app returned it, which on
    a public URL meant anyone could open DevTools and spend the owner's quota.
    """
    defaults = settings.public_llm_defaults
    return {
        # provider defaults are safe to share (they are not credentials)
        "llm_base_url": defaults["llm_base_url"],
        "llm_model": defaults["llm_model"],
        # the browser only learns WHETHER a server key exists
        "has_server_key": settings.server_llm_ready and settings.allow_server_llm_fallback,
        "require_user_key": not (settings.server_llm_ready and settings.allow_server_llm_fallback),
        # limits the UI should display honestly
        "max_posts_per_scan": settings.max_posts_per_scan,
        "max_lookback_days": settings.max_lookback_days,
        "batch_size": settings.batch_size,
        "rate_limit_scans_per_hour": settings.rate_limit_scans_per_hour,
        "telegram_mode": settings.describe()["telegram_mode"],
        "telethon_ready": settings.telethon_ready,
    }


@app.post("/plan")
async def plan(req: PlanRequest):
    """Dry run: 'if I scan from this date, how much work is that?'  Costs nothing."""
    try:
        return describe_plan(req.since, req.tz_offset_minutes)
    except (WindowError, ValidationError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/models")
async def list_models(req: ModelsRequest, request: Request):
    """Ask the provider which models this key can use *today*.

    Model names are retired constantly, so the UI fetches the real list instead
    of relying on a hardcoded one. It also validates the key and URL before the
    user spends a scan on them.
    """
    ip = client_ip(request)
    # Cheap separate limiter so a visitor cannot hammer providers through us.
    ok, reason = _model_probe.check_and_reserve(ip)
    if not ok:
        raise HTTPException(429, reason)
    try:
        base_url = validate_llm_base_url(
            req.llm_base_url, allow_insecure=settings.allow_insecure_llm_urls
        )
        api_key = validate_api_key(req.llm_api_key)
        result = await fetch_models(base_url, api_key)
        logger.info("Listed %s model(s) for %s", result["total"], ip)
        return result
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(400, str(exc)) from exc
    finally:
        _model_probe.release(ip)


@app.post("/search")
async def start_search(req: SearchRequest, request: Request):
    """Validate everything up front, then hand off to a background scan."""
    ip = client_ip(request)

    # --- input validation (fail fast, in the user's language) ---------------
    try:
        channel = validate_channel(req.channel)
        criteria = validate_criteria(req.criteria)
        if not req.since.strip():
            raise ValidationError("A 'since' date is required")
        base_url = validate_llm_base_url(
            req.llm_base_url or settings.llm_base_url,
            allow_insecure=settings.allow_insecure_llm_urls,
        )
        model = validate_model(req.llm_model or settings.llm_model)
        api_key = (req.llm_api_key or "").strip()
        use_server_key = False

        if not api_key:
            if settings.server_llm_ready and settings.allow_server_llm_fallback:
                api_key = settings.llm_api_key  # never leaves the server
                use_server_key = True
            else:
                raise ValidationError(
                    "Your own LLM API key is required. Open 'Use your own API key' and paste it — "
                    "it stays in your browser and is only used for your scans."
                )
        else:
            api_key = validate_api_key(api_key)
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc

    # --- abuse guards -------------------------------------------------------
    ok, reason = limiter.check_and_reserve(ip)
    if not ok:
        raise HTTPException(429, reason)
    if not gate.try_acquire():
        limiter.release(ip)
        raise HTTPException(
            503, "The server is handling as many scans as it can right now. Try again in a minute."
        )

    job = store.create(
        channel=channel,
        criteria=criteria,
        since_raw=req.since,
        tz_offset_minutes=req.tz_offset_minutes,
        ip=ip,
    )

    def on_finish(_job):
        gate.release()
        limiter.release(ip)

    store.attach(
        job,
        run_scan(
            job,
            llm_base_url=base_url,
            llm_api_key=api_key,
            llm_model=model,
            on_finish=on_finish,
        ),
    )

    logger.info(
        "Scan %s started by %s (channel=%s, windows planned on the fly, server_key=%s)",
        job.id, ip, channel, use_server_key,
    )
    return {"job_id": job.id, "channel": channel, "using_server_key": use_server_key}


@app.get("/search/{job_id}")
async def search_status(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "That scan has expired (server restarted or 10 minutes passed). Run it again.")
    return job.public()


@app.post("/search/{job_id}/cancel")
async def cancel_search(job_id: str):
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Unknown scan")
    task = store.get_task(job_id)
    if store.cancel(job_id) and task is not None:
        task.cancel()
        return {"cancelled": True}
    return {"cancelled": False, "stage": job.stage}


@app.exception_handler(Exception)
async def unhandled(_request: Request, exc: Exception):
    logger.exception("Unhandled error")
    return JSONResponse(status_code=500, content={"detail": "Unexpected server error."})
