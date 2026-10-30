"""Calls any OpenAI-compatible /chat/completions endpoint and asks one question
per post: is this a genuine match for the user's criteria, and is it expired?

Changes vs the original local version (everything else is deliberately kept):
  * the caller's API key is required and is never logged or echoed;
  * ids are compared as strings, so "123" from the model matches int 123;
  * the response must be a JSON array, otherwise the batch is retried once
    with a stricter reminder instead of failing the whole scan;
  * `content: null` (reasoning models) is handled instead of crashing;
  * the system prompt is unchanged in substance — it works well;
  * retries: 429/5xx with exponential backoff, plus one "repair" retry for
    invalid JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from datetime import date

import httpx

logger = logging.getLogger("job_filter")

# 429/5xx are *transient*: the provider is busy or throttling us, not rejecting
# the request. They are retried with jittered exponential backoff. 15s for the
# first gap and 60s of total patience matters because a Google capacity spike
# ("503 UNAVAILABLE, high demand") lasts 30s to a few minutes: the old 2/4/8s
# ladder gave up after 14s, i.e. always inside the window.
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BASE_DELAY_SECONDS = 3       # with jitter: ~2-3s, 4-6s, 8-12s per gap
# Patience per request. Kept short on purpose: a batch that is still refused
# after ~25s is split in half instead of waited on, and splitting succeeds far
# more often than waiting. With a total outage the worst case is therefore
# ~3 waves x 25s rather than several minutes of the visitor watching a spinner.
MAX_TOTAL_BACKOFF_SECONDS = 20
REQUEST_TIMEOUT_SECONDS = 120

# Seen on free Gemini keys. Decided server-side so the visitor is never asked to
# understand throttling or choose a fallback model.
FALLBACK_MODEL_HINTS = ("flash-lite", "flash", "lite", "mini")

# Which kind of failure this is, so app/jobs.py can decide whether to split the
# batch and what the visitor should be told.
KIND_TRANSIENT = "transient"      # 429 / 503 / timeout — temporary, retry helps
KIND_PERMANENT = "permanent"      # 401 / 402 / 404 / 400 — retrying cannot help
KIND_UNKNOWN = "unknown"

SYSTEM_PROMPT = """You are a strict job-post relevance filter. You receive today's
date, a user's search criteria, and a batch of Telegram channel posts.

Your job is to DISCARD almost everything. A post is only a match if a specific
position in it is one the user could actually be hired for. When in doubt, the
post is NOT a match.

Work through these gates IN ORDER for every post. Stop at the first gate that
fails and drop the post.

=== GATE 0 - DEADLINE ===
Find any stated deadline ("Deadline", "Closing date", "Apply before", or a date
near the top or bottom).
- Stated AND before today -> EXPIRED. Report "expired", never a match.
- Stated and today or later -> continue.
- Not stated -> continue. Do not invent one.

=== GATE 1 - REAL JOB? ===
Is this an actual vacancy notice with a named position? Ads, event promos,
greetings, news, reposts of other channels' adverts -> DROP.
A post with no identifiable position title -> DROP.

=== GATE 2 - FIELD ALIGNMENT (the most important gate) ===
Identify the user's field from their criteria and the DEGREE they hold,
e.g. "electrical engineer", "computer science", "accounting", "nursing".

Then read each position's REQUIRED qualification and decide:

IN RANGE - the qualification explicitly names the user's field or a genuinely
  interchangeable sibling of it. Examples: user is "Electrical & Computer
  Engineering" -> "Electrical Engineering", "Computer Engineering",
  "Electrical and Computer Engineering", "Electronics Engineering",
  "Computer Science", "Software Engineering", "IT" are IN RANGE.

OUT OF RANGE - the qualification names a DIFFERENT discipline. Examples for
  that same user: Accounting, Banking & Finance, Economics, Management,
  Business Administration, Marketing, Sales, Nursing, Public Health,
  Pharmacy, Law, Social Work, Sociology, Psychology, Development Studies,
  Political Science, International Relations, Communications, Journalism,
  Civil Engineering, Mechanical Engineering, Automotive Mechanics,
  Auto Electrician, Industrial/Manufacturing Technology, TVET Level III
  welding or machining, Graphic Design, Secretarial Science, Logistics,
  Procurement, Supply Chain, Human Resources. -> DROP.

THE "RELATED FIELD" TRAP - READ THIS CAREFULLY:
Ethiopian job posts constantly end their qualification list with "or any
related field", "or other related fields", "related discipline". This phrase
is a legal catch-all. It does NOT mean every degree qualifies, and it does NOT
make an accounting job relevant to an engineer.
- If the list of named fields contains NOTHING in the user's field, the post is
  OUT OF RANGE. The phrase "or related field" does not save it.
- Accept "related field" ONLY when the named fields are already in the user's
  family of study.
Count this explicitly: if you cannot point to the user's degree (or a true
sibling of it) named in the text, DROP the post.

MULTI-POSITION POSTS:
Posts often list several positions. Evaluate each one separately.
- Keep the post ONLY if at least one position passes Gate 2.
- In that case still apply Gate 3 to that specific position.
- Name the passing position in your reason, e.g. "Position 2: Junior
  Electrical Engineer fits your degree and requires 1 year."
- If every position fails, DROP the whole post.

=== GATE 3 - SENIORITY ===
For the position(s) that passed Gate 2, check the stated experience.
- User asks for fresh graduate / 0 years: accept positions that state 0
  years, "fresh graduate", "no experience required", "entry level", or an
  internship/trainee role.
- REJECT if the position explicitly requires 1 or more years, OR its title is
  Senior / Head / Lead / Principal / Director / Manager / Expert / Officer
  paired with a years requirement, OR it is a Deputy GM, Regional Director,
  Branch Manager and similar. "Junior Accountant" is fine; "Accountant (1 year)"
  is not, unless the post separately offers a 0-year junior role.
- A perfect field match at the wrong seniority is still NOT a match.

=== GATE 4 - LOCATION / OTHER HARD REQUIREMENTS ===
If the user named a city or region and the post states a different one, DROP.
If the post requires something the user plainly lacks and states it as
mandatory (a licence, a specific gender, a professional certification),
DROP.

=== OUTPUT ===
Return "high" only when Gates 0-4 all pass cleanly and the field is a direct,
named match.
Return "medium" ONLY for a borderline seniority or location detail where the
field itself is clearly correct - for example a junior role in the user's
field that also mentions a 1-year preference, or a strong field match whose
degree list is ambiguous between two closely related engineering branches.
NEVER use "medium" to keep a post whose field is a different discipline. A
wrong-discipline post is always DROP, never "medium".

Your reason must state the concrete evidence you relied on, quoting the degree
or experience line, e.g. "BSc Electrical Engineering, 0 years - matches."
or for a rejection you do not report, simply omit the post.

Return ONLY a JSON array, one object per post that is either a match OR
expired, in this exact shape:
[
  {"id": <post id, same type as given>, "status": "high" | "medium" | "expired", "reason": "<one short sentence quoting the degree/experience you found>", "position": "<the position title that matched, or empty>"}
]
For expired posts use status "expired" and reason "Deadline was <date> - expired."
Use the id EXACTLY as provided (do not change type or formatting).
OMIT every post that is not a match and not expired. When unsure, omit it.
Do not include any text outside the JSON array."""

REPAIR_SUFFIX = (
    "\n\nIMPORTANT: your previous reply could not be parsed. Reply with ONLY the JSON "
    "array — no markdown fences, no commentary, no trailing text."
)


class LLMError(Exception):
    """User-facing LLM failure (bad key, wrong model, provider down...).

    `kind` tells the caller whether this is worth retrying in another shape:
    a "transient" failure (429, 503, timeout) often succeeds on a smaller batch
    or a lighter model, whereas "permanent" ones (bad key) never will.
    """

    def __init__(self, message: str, kind: str = KIND_UNKNOWN, status: int = 0,
                 retry_after: float = 0.0, unread: int = 0):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after = retry_after
        # How many posts went unclassified. Set by the caller that knows the
        # batch size, so accounting survives a batch being split in half.
        self.unread = unread


def build_user_message(criteria: str, posts_batch: list[dict]) -> str:
    slim = [{"id": p["id"], "text": p.get("text", "")} for p in posts_batch]
    return (
        f"Today's date: {date.today().isoformat()}\n\n"
        f"User criteria: {criteria}\n\n"
        f"Posts to evaluate (JSON array):\n{json.dumps(slim, ensure_ascii=False)}"
    )


def _extract_json(raw: str) -> list[dict]:
    """Parse the model's reply into a list, tolerating fences and stray prose."""
    if raw is None:
        raise LLMError("The model returned an empty message (content was null).")
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
        text = text.strip()

    for candidate in (text, _slice_array(text)):
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            parsed = parsed.get("results") or parsed.get("matches") or [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        raise LLMError("The model returned JSON but not an array of results.")

    raise LLMError(f"The model did not return valid JSON: {text[:200]}")


def _slice_array(text: str) -> str | None:
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        return None
    return text[start : end + 1]


async def classify_batch(
    base_url: str,
    api_key: str,
    model: str,
    criteria: str,
    posts_batch: list[dict],
) -> list[dict]:
    """Classify one batch. Raises LLMError with a human-readable message."""
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(criteria, posts_batch)},
    ]

    last_error: Exception | None = None
    waited = 0.0

    for attempt in range(1, MAX_RETRIES + 1):
        payload = {"model": model, "messages": messages, "temperature": 0}
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as http:
                resp = await http.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body = (exc.response.text or "")[:400]
            logger.error("LLM HTTP %s: %s", status, body)
            if status in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                # Honour the provider's own answer when it sends one; otherwise
                # back off exponentially *with jitter*, so concurrent batches
                # stop retrying in lockstep and re-colliding.
                delay = _retry_after(exc.response) or _jittered(BASE_DELAY_SECONDS, attempt)
                if waited + delay > MAX_TOTAL_BACKOFF_SECONDS:
                    raise LLMError(_friendly_http_error(status, body),
                                   kind=KIND_TRANSIENT, status=status) from exc
                waited += delay
                logger.warning("LLM %s — retrying in %.1fs (%s/%s)", status, delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue
            raise LLMError(_friendly_http_error(status, body),
                           kind=_kind_for_status(status), status=status) from exc
        except httpx.TimeoutException as exc:
            if attempt < MAX_RETRIES:
                delay = _jittered(BASE_DELAY_SECONDS, attempt)
                if waited + delay > MAX_TOTAL_BACKOFF_SECONDS:
                    raise LLMError("The AI provider did not answer in time.",
                                   kind=KIND_TRANSIENT) from exc
                waited += delay
                logger.warning("LLM timeout — retrying in %.1fs (%s/%s)", delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue
            raise LLMError("The AI provider did not answer in time.",
                           kind=KIND_TRANSIENT) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the AI provider: {exc}",
                           kind=KIND_TRANSIENT) from exc

        try:
            choice = (data.get("choices") or [{}])[0]
            content = (choice.get("message") or {}).get("content")
            return _extract_json(content)
        except LLMError as exc:
            last_error = exc
            if attempt == 1:  # one repair attempt, then give up
                logger.warning("Unparseable LLM reply, asking for a JSON-only retry.")
                messages = messages + [
                    {"role": "user", "content": REPAIR_SUFFIX},
                ]
                continue
            raise

    raise LLMError(str(last_error) if last_error else "The AI call failed.", kind=KIND_TRANSIENT)


def _jittered(base: float, attempt: int) -> float:
    """Exponential backoff that is never the same twice.

    Without the random factor every batch sleeps for exactly the same time and
    they all wake (and get rate-limited) on the same tick.
    """
    return base * (2 ** (attempt - 1)) * random.uniform(0.6, 1.0)


def _retry_after(response: httpx.Response) -> float:
    """Seconds the provider asked us to wait, or 0 when it did not say."""
    raw = response.headers.get("retry-after") or response.headers.get("x-ratelimit-reset")
    if not raw:
        return 0.0
    try:
        return max(0.0, min(float(raw), MAX_TOTAL_BACKOFF_SECONDS))
    except ValueError:
        return 0.0


def _kind_for_status(status: int) -> str:
    if status in RETRYABLE_STATUS_CODES:
        return KIND_TRANSIENT
    if status in (401, 402, 403, 404, 400):
        return KIND_PERMANENT
    return KIND_UNKNOWN


def pick_fallback_model(models: list[str], current: str) -> str:
    """First live model that is cheaper/lighter than the one that just failed.

    Picked from the provider's own /models list, so nothing is hardcoded and
    nothing goes stale when a model is retired.
    """
    cur = (current or "").lower()
    for hint in FALLBACK_MODEL_HINTS:
        for m in models:
            low = m.lower()
            if hint in low and low != cur and "preview" not in low:
                return m
    return ""


def _friendly_http_error(status: int, body: str) -> str:
    if status in (401, 403):
        return "The API key was rejected (401/403). Check that the key belongs to this provider."
    if status == 402:
        return "The provider says the account has no credit / quota left."
    if status == 404:
        return (
            "404 from the provider — usually a wrong base URL or a model name that does not "
            "exist for this provider. Base URL and model must belong to the same provider."
        )
    if status == 400:
        return f"The provider rejected the request (400). Common cause: model name mismatch. Details: {body[:200]}"
    if status == 429:
        return ("The AI key hit its request limit for now. We slowed down and retried "
                "but it was still over the limit.")
    if status == 503:
        # Google's "model is currently experiencing high demand" lands here.
        # Never show the visitor the raw provider JSON — it is not actionable.
        return ("The AI provider was overloaded and turned the request away. "
                "This is temporary and on their side.")
    if status == 500:
        return "The AI provider had an internal error. This is on their side."
    if status in (502, 504):
        return "The AI provider could not answer in time. This is on their side."
    return f"LLM provider error {status}. {_short_reason(body)}"


def _short_reason(body: str, limit: int = 120) -> str:
    """Pull the provider's own explanation out of a JSON error body.

    A raw payload such as {"error":{"code":503,...}} is never user-facing, but
    the sentence inside it sometimes is, so extract just that.
    """
    try:
        payload = json.loads(body)
        err = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or err.get("status") or ""
        else:
            msg = payload.get("message", "") if isinstance(payload, dict) else ""
        msg = str(msg).strip()
        return (msg[:limit] + "…") if len(msg) > limit else msg
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Live model discovery
# --------------------------------------------------------------------------- #
# Model names go stale fast: gemini-2.5-flash was retired 20 Oct 2026, and
# Groq's Llama models moved to enterprise-only. Rather than ship a list that
# rots, ask the provider what it currently serves (GET {base}/models).
_NOT_CHAT_MODEL_HINTS = (
    "embedding", "embed", "imagen", "veo", "tts", "whisper",
    "image", "audio", "live", "native-audio", "computer-use",
    "prompt-guard", "guard", "rerank", "moderation", "aqa",
    # "preview" models carry no availability promise and are the first to be
    # shed when a free tier is under load — they produced most of the 503s.
    "preview",
)

_PREFERRED_ORDER = (
    "flash-lite", "flash-lite-preview", "flash", "gpt-oss-20b", "gpt-oss-120b",
    "llama-3.1-8b", "qwen", "llama", "kimi", "compound",
    "pro", "argon",
)


def _looks_like_chat_model(model_id: str) -> bool:
    low = model_id.lower()
    return not any(hint in low for hint in _NOT_CHAT_MODEL_HINTS)


def _rank(model_id: str) -> tuple[int, str]:
    low = model_id.lower()
    for idx, needle in enumerate(_PREFERRED_ORDER):
        if needle in low:
            return idx, low
    return len(_PREFERRED_ORDER), low


async def fetch_models(base_url: str, api_key: str, limit: int = 40) -> dict:
    """Return the model IDs this key can actually use right now.

    Returns {"models": [...], "total": n, "note": "..."}.
    Raises LLMError with a friendly message when the key or URL is wrong —
    which doubles as key validation before the user spends a scan.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=25) as http:
            resp = await http.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach the provider to list models: {exc}") from exc

    if resp.status_code in (401, 403):
        raise LLMError("That key was rejected when listing models (401/403). Check you pasted it fully and that it belongs to this provider.")
    if resp.status_code == 404:
        raise LLMError("This provider has no /models endpoint at that base URL. Type the model name manually.")

    if resp.status_code >= 400:
        raise LLMError(f"The provider returned {resp.status_code} when listing models.")

    try:
        payload = resp.json()
    except ValueError as exc:
        raise LLMError("The provider did not return JSON for the model list.") from exc

    raw = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        raise LLMError("Unexpected model-list format from the provider.")

    ids: list[str] = []
    for entry in raw:
        if isinstance(entry, dict):
            mid = entry.get("id") or entry.get("name")
        else:
            mid = entry
        if not isinstance(mid, str):
            continue
        mid = mid.removeprefix("models/").strip()
        if mid and mid not in ids:
            ids.append(mid)

    chat_models = [m for m in ids if _looks_like_chat_model(m)]
    chosen = chat_models or ids
    chosen.sort(key=_rank)

    note = ""
    if not chat_models and ids:
        note = "Only non-chat models were returned; showing everything."
    if len(chosen) > limit:
        note = f"Showing the {limit} most relevant of {len(chosen)} models."

    return {"models": chosen[:limit], "total": len(chosen), "note": note}
