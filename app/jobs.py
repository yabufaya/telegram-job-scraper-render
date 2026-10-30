"""In-memory job registry + the scan runner.

There is NO database and NO disk state here. A job is a plain object in a dict;
it disappears when the process restarts or when its TTL expires (10 minutes
after the last poll). That is exactly the "save nothing after the page is
closed" behaviour you asked for.

Why jobs at all instead of one long HTTP request?
  * Render free instances can cold-start for ~60s; a 3-minute scan inside one
    request would look frozen and would be killed if the browser tab closed.
  * The UI polls /search/{job_id} every 1.5s and renders real progress:
    "window 2/3 · batch 7/21".
  * Cancelling is possible (the user stops paying for tokens).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app import pipeline
from app.config import settings
from app.llm_adapter import LLMError, classify_batch
from app.sources import PreviewUnavailable, SourceError, fetch_window
from app.window import (
    ScanWindow,
    WindowError,
    clamp_lookback,
    human_span,
    parse_since,
    plan_windows,
)

logger = logging.getLogger("job_filter")

JOB_TTL_SECONDS = 600          # forget finished jobs after 10 min
MAX_JOB_SECONDS = 20 * 60      # hard ceiling per scan


@dataclass
class Job:
    id: str
    channels: list[str]
    criteria: str
    since_raw: str
    tz_offset_minutes: int
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    stage: str = "queued"       # queued|planning|fetching|classifying|done|error|cancelled
    message: str = ""
    error: str = ""

    windows_total: int = 0
    windows_done: int = 0
    current_window: str = ""

    posts_fetched: int = 0
    posts_scanned: int = 0
    duplicates_removed: int = 0
    batches_total: int = 0
    batches_done: int = 0
    batches_failed: int = 0
    posts_unread: int = 0          # posts a failed batch could not classify
    expired_count: int = 0
    # True when *no* batch survived: the scan produced no evidence either way,
    # which must never be reported as "no matches".
    nothing_read: bool = False
    failure_kind: str = ""         # transient | permanent | unknown

    results: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    sources_used: set[str] = field(default_factory=set)
    windows: list[dict] = field(default_factory=list)
    cursor: dict | None = None
    clamped_from: str | None = None
    cancelled: bool = False
    started: float = 0.0
    finished: float = 0.0

    def touch(self) -> None:
        self.updated_at = time.time()

    def public(self) -> dict:
        elapsed = (self.finished or time.time()) - (self.started or self.created_at)
        return {
            "job_id": self.id,
            "stage": self.stage,
            "message": self.message,
            "error": self.error,
            "channels": self.channels,
            "channel": ", ".join(self.channels),
            "windows_total": self.windows_total,
            "windows_done": self.windows_done,
            "current_window": self.current_window,
            "windows": self.windows,
            "posts_fetched": self.posts_fetched,
            "posts_scanned": self.posts_scanned,
            "duplicates_removed": self.duplicates_removed,
            "batches_total": self.batches_total,
            "batches_done": self.batches_done,
            "batches_failed": self.batches_failed,
            "posts_unread": self.posts_unread,
            "nothing_read": self.nothing_read,
            "failure_kind": self.failure_kind,
            "expired_count": self.expired_count,
            "results": self.results,
            "warnings": self.warnings,
            "sources_used": sorted(self.sources_used),
            "cursor": self.cursor,
            "clamped_from": self.clamped_from,
            "elapsed_seconds": round(elapsed, 1),
            "done": self.stage in {"done", "error", "cancelled"},
        }


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._by_ip: dict[str, int] = {}

    def create(self, *, channels: list[str], criteria: str, since_raw: str, tz_offset_minutes: int, ip: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex[:12],
            channels=channels,
            criteria=criteria,
            since_raw=since_raw,
            tz_offset_minutes=tz_offset_minutes,
        )
        self._jobs[job.id] = job
        self._by_ip[job.id] = hash(ip) & 0xFFFFFFFF
        self._gc()
        return job

    def get(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job:
            job.touch()
        return job

    def get_task(self, job_id: str):
        return self._tasks.get(job_id)

    def attach(self, job: Job, coro) -> None:
        self._tasks[job.id] = asyncio.create_task(coro)

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.stage in {"done", "error", "cancelled"}:
            return False
        job.cancelled = True
        job.touch()
        return True

    def _gc(self) -> None:
        now = time.time()
        stale = [
            jid
            for jid, job in self._jobs.items()
            if job.stage in {"done", "error", "cancelled"} and now - job.updated_at > JOB_TTL_SECONDS
        ]
        for jid in stale:
            self._jobs.pop(jid, None)
            self._tasks.pop(jid, None)
            self._by_ip.pop(jid, None)

    @property
    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if j.stage not in {"done", "error", "cancelled"})


store = JobStore()


# --------------------------------------------------------------------------- #
# The scan itself
# --------------------------------------------------------------------------- #
def _failure_sentence(kind: str, unread: int, scanned: int) -> str:
    """One line about posts we could not read — no jargon, no provider JSON.

    Deliberately contains no advice the visitor cannot act on: telling someone
    to "lower concurrency" is a note to us in the log, not usable help.
    """
    head = f"{unread} of {scanned} post(s) could not be read"
    if kind == "permanent":
        return f"{head} — the AI key or model was rejected, so retrying the same scan will not help."
    if kind == "transient":
        return (f"{head} — the AI provider was busy or rate-limiting. "
                f"Retrying, or scanning a shorter date range, usually gets them.")
    return f"{head}."


async def run_scan(
    job: Job,
    *,
    llm_base_url: str,
    llm_api_key: str,
    llm_model: str,
    on_finish=None,
) -> None:
    """Fetch -> dedupe -> window -> batch -> classify, updating `job` as it goes."""
    started = time.time()
    job.started = started
    try:
        await _run(job, llm_base_url, llm_api_key, llm_model, started)
    except asyncio.CancelledError:
        job.stage = "cancelled"
        job.message = "Scan cancelled."
        raise
    except (WindowError, SourceError, LLMError) as exc:
        job.stage = "error"
        job.error = str(exc)
        job.message = "Scan failed."
        logger.warning("Scan %s failed: %s", job.id, exc)
    except Exception as exc:  # noqa: BLE001 - never leak a traceback to the user
        job.stage = "error"
        job.error = f"Unexpected error: {type(exc).__name__}"
        job.message = "Scan failed."
        logger.exception("Scan %s crashed", job.id)
    finally:
        job.finished = time.time()
        job.current_window = ""
        job.touch()
        if on_finish:
            on_finish(job)


async def _run(job: Job, base_url: str, api_key: str, model: str, started: float) -> None:
    # 1. Plan -----------------------------------------------------------------
    job.stage = "planning"
    job.message = "Planning the date range…"
    since = parse_since(job.since_raw, job.tz_offset_minutes)
    since, was_clamped = clamp_lookback(since, settings.max_lookback_days)
    if was_clamped:
        job.clamped_from = job.since_raw
        job.warnings.append(
            f"Only the last {settings.max_lookback_days} days are scanned per run. "
            f"Use 'Scan earlier' to continue further back, or raise MAX_LOOKBACK_DAYS."
        )

    windows: list[ScanWindow] = plan_windows(since, window_days=14)
    job.windows_total = len(windows)
    job.windows = [
        {"index": w.index, "label": w.label, "status": "pending"} for w in windows
    ]
    logger.info(
        "Scan %s: %s -> now = %s window(s) of 14 days",
        job.id,
        since.date(),
        len(windows),
    )

    # 2. Fetch each window ----------------------------------------------------
    job.stage = "fetching"
    all_posts: list[dict] = []
    fetch_failures: list[str] = []

    multi = len(job.channels) > 1
    for window in windows:
        if job.cancelled or time.time() - started > MAX_JOB_SECONDS:
            job.stage = "cancelled" if job.cancelled else "done"
            break

        job.current_window = window.label
        job.windows[window.index]["status"] = "fetching"
        window_posts = 0
        channel_failures = 0

        for channel in job.channels:
            if job.cancelled or time.time() - started > MAX_JOB_SECONDS:
                break

            remaining = max(0, settings.max_posts_per_scan - len(all_posts))
            if remaining <= 0:
                job.warnings.append(
                    f"Stopped at the {settings.max_posts_per_scan}-post cap. Narrow the date range "
                    f"or scan in two passes."
                )
                job.windows[window.index]["status"] = "skipped"
                break

            job.message = (
                f"Reading @{channel} · {window.label}…" if multi
                else f"Reading {window.label}…"
            )
            job.current_window = f"@{channel} · {window.label}" if multi else window.label

            try:
                posts, source = await fetch_window(channel, window, max_posts=remaining)
                job.sources_used.add(source)
                all_posts.extend(posts)
                window_posts += len(posts)
                job.posts_fetched = len(all_posts)
                job.windows[window.index]["status"] = "done"
            except PreviewUnavailable as exc:
                channel_failures += 1
                fetch_failures.append(f"@{channel}: {exc}")
                job.warnings.append(
                    f"@{channel} · {window.label}: {exc} (no Telegram session configured)"
                )
            except SourceError as exc:
                channel_failures += 1
                fetch_failures.append(f"@{channel}: {exc}")
                job.warnings.append(f"@{channel} · {window.label}: {exc}")

            job.touch()

        job.windows[window.index]["posts"] = window_posts
        if channel_failures and channel_failures == len(job.channels):
            job.windows[window.index]["status"] = "failed"
        job.windows_done += 1
        job.touch()

    if not all_posts:
        if job.cancelled:
            raise asyncio.CancelledError
        if fetch_failures:
            raise SourceError(fetch_failures[0])
        job.stage = "done"
        job.message = "No posts found in that date range."
        return

    # 3. Clean + de-duplicate -------------------------------------------------
    #    Sorting newest-first means the UI reads naturally, and the cursor we
    #    send back is the oldest post we actually covered.
    unique_posts, removed = pipeline.dedupe_posts(all_posts, by_text=settings.dedupe_by_text)
    job.duplicates_removed = removed
    unique_posts = pipeline.sort_newest_first(unique_posts)
    job.posts_scanned = len(unique_posts)

    # Ensure every post is inside exactly one window (no cross-window bleed).
    buckets = pipeline.assign_to_windows(unique_posts, windows)
    ordered: list[dict] = []
    for idx in sorted(buckets, reverse=True):  # newest window first
        ordered.extend(buckets[idx])

    # 4. Batch ---------------------------------------------------------------
    batches = pipeline.build_batches(
        ordered,
        batch_size=settings.batch_size,
        max_batch_chars=settings.max_batch_chars,
        max_chars_per_post=settings.max_chars_per_post,
    )
    job.batches_total = len(batches)
    job.stage = "classifying"
    job.message = f"Classifying {len(ordered)} post(s) in {len(batches)} batch(es)…"
    logger.info(
        "Scan %s: %s unique post(s), %s batch(es), max %s in flight",
        job.id, len(ordered), len(batches), settings.max_concurrent_llm_calls,
    )

    # 5. Classify ------------------------------------------------------------
    semaphore = asyncio.Semaphore(settings.max_concurrent_llm_calls)
    results: list[dict] = []
    warnings: list[str] = []

    async def classify_resilient(batch: list[dict], depth: int = 0):
        """Classify a batch, splitting it in half when the provider says no.

        A 429 or a 503 is a statement about *size and speed*, not about content:
        15 posts in one request is a big token burst that a free tier refuses,
        while two requests of 7 usually go through. So on a transient error the
        batch is halved and each half retried once. Only what still fails after
        that is reported as unread — the old behaviour threw the whole batch
        away, which is how a scan of 84 posts produced 0 matches.

        Permanent failures (bad key, wrong model) are not split: the same key
        will fail the same way, so splitting only wastes the visitor's quota.
        """
        try:
            return await classify_batch(base_url, api_key, model, job.criteria, batch)
        except LLMError as exc:
            job.failure_kind = job.failure_kind or exc.kind
            if exc.kind != "transient" or len(batch) < 2 or depth >= 1:
                raise
            mid = len(batch) // 2
            logger.info(
                "Scan %s: %s — splitting %s post(s) into %s + %s and retrying",
                job.id, exc.kind, len(batch), mid, len(batch) - mid,
            )
            halves = []
            lost = 0
            for part in (batch[:mid], batch[mid:]):
                try:
                    halves.extend(await classify_resilient(part, depth + 1))
                except LLMError as sub:
                    lost += len(part)
                    job.failure_kind = job.failure_kind or sub.kind
                    logger.warning("Scan %s: half of a batch also failed: %s", job.id, sub)
            if halves:
                if lost:
                    # Partial recovery: the posts we did get are returned, and
                    # the ones we did not are counted by the caller.
                    job.posts_unread += lost
                return halves
            exc.unread = len(batch)
            raise

    async def one(batch_no: int, batch: list[dict]):
        async with semaphore:
            if job.cancelled:
                raise asyncio.CancelledError
            items = await classify_resilient(batch)
            merged, expired, warns = pipeline.merge_batch_results(items, batch, criteria=job.criteria)
            return batch_no, merged, expired, warns

    tasks = [asyncio.create_task(one(i + 1, b)) for i, b in enumerate(batches)]

    try:
        for coro in asyncio.as_completed(tasks):
            if job.cancelled:
                break
            try:
                batch_no, merged, expired, warns = await coro
                results.extend(merged)
                job.expired_count += expired
                warnings.extend(warns)
                job.batches_done += 1
            except asyncio.CancelledError:
                raise
            except LLMError as exc:
                job.batches_failed += 1
                job.posts_unread += exc.unread
                job.failure_kind = job.failure_kind or exc.kind
                warnings.append(f"__failed__:{exc.kind}:{exc}")
                logger.warning("Scan %s: %s", job.id, exc)
            except Exception as exc:  # noqa: BLE001
                job.batches_failed += 1
                warnings.append(f"__failed__:unknown:{type(exc).__name__}")
                logger.exception("Scan %s: batch crashed", job.id)
            job.message = (
                f"Classifying… {job.batches_done + job.batches_failed}/{job.batches_total} batches"
            )
            job.touch()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()

    if job.cancelled:
        job.stage = "cancelled"
        job.message = "Scan cancelled — no further tokens spent."
        return

    # 6. Finish --------------------------------------------------------------
    final = pipeline.dedupe_results(results)
    job.results = pipeline.sort_results(final)
    # Collapse the per-batch field-gate counters into one line the visitor can read.
    field_dropped = 0
    kept: list[str] = []
    for w in warnings:
        if w.startswith("__field_dropped__:"):
            field_dropped += int(w.split(":", 1)[1])
        else:
            kept.append(w)
    if field_dropped:
        kept.insert(
            0,
            f"{field_dropped} post(s) were dropped because the posted field did not "
            f"match yours.",
        )

    # Turn the opaque "Batch failed: ..." lines into one sentence a visitor can
    # understand, and keep the technical reason only for the server log. The
    # visitor is never asked to change concurrency or pick a model — those are
    # our decisions, not theirs.
    failures = [w for w in kept if w.startswith("__failed__:")]
    kept = [w for w in kept if not w.startswith("__failed__:")]
    job.nothing_read = bool(job.batches_total) and job.posts_unread >= job.posts_scanned > 0
    if failures and not job.nothing_read:
        kind = failures[0].split(":", 2)[1]
        kept.insert(0, _failure_sentence(kind, job.posts_unread, job.posts_scanned))
    job.warnings.extend(kept)
    job.cursor = pipeline.make_cursor(unique_posts)
    job.stage = "done"
    took = int(time.time() - started)
    if job.nothing_read:
        # Nothing was classified, so there is no evidence about matching at all.
        # Saying "0 matches" here is what sent people off to rewrite a profile
        # that was never tested.
        job.message = (
            f"Stopped after {took}s — none of the {job.posts_scanned} post(s) could be read. "
            f"Nothing was checked, so this is not a matching result."
        )
    elif job.posts_unread:
        job.message = (
            f"Done in {took}s — {len(job.results)} match(es) from {job.posts_scanned} post(s); "
            f"{job.posts_unread} post(s) could not be read."
        )
    else:
        job.message = (
            f"Done in {took}s — {len(job.results)} match(es) from {job.posts_scanned} post(s)"
            f"{f' ({job.duplicates_removed} duplicate(s) dropped)' if job.duplicates_removed else ''}."
        )
    job.current_window = ""
    logger.info(
        "Scan %s done: %s match(es), %s expired, %s batch failure(s), %s duplicate(s)",
        job.id, len(job.results), job.expired_count, job.batches_failed, job.duplicates_removed,
    )


def describe_plan(since_raw: str, tz_offset_minutes: int) -> dict:
    """Dry-run used by the UI to show what a scan will cost before running it."""
    since = parse_since(since_raw, tz_offset_minutes)
    since, clamped = clamp_lookback(since, settings.max_lookback_days)
    windows = plan_windows(since, window_days=14)
    now = datetime.now(timezone.utc)
    return {
        "since_utc": since.isoformat(),
        "span": human_span(now - since),
        "windows": [{"index": w.index + 1, "label": w.label} for w in windows],
        "window_count": len(windows),
        "clamped": clamped,
        "max_posts": settings.max_posts_per_scan,
        "batch_size": settings.batch_size,
        "max_batches": (settings.max_posts_per_scan + settings.batch_size - 1) // settings.batch_size,
        # Rough ceiling: assume ~12s per batch, spread over the concurrency limit.
        "estimate_seconds": int(
            ((settings.max_posts_per_scan / settings.batch_size) / settings.max_concurrent_llm_calls) * 12
        ),
        "max_concurrent_llm_calls": settings.max_concurrent_llm_calls,
        "now_utc": now.isoformat(),
    }
