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
from datetime import date

import httpx

logger = logging.getLogger("job_filter")

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4
BASE_DELAY_SECONDS = 2  # 2s, 4s, 8s, 16s
REQUEST_TIMEOUT_SECONDS = 120

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
    """User-facing LLM failure (bad key, wrong model, provider down...)."""


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
                delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning("LLM %s — retrying in %ss (%s/%s)", status, delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue
            raise LLMError(_friendly_http_error(status, body)) from exc
        except httpx.TimeoutException as exc:
            if attempt < MAX_RETRIES:
                delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                logger.warning("LLM timeout — retrying in %ss (%s/%s)", delay, attempt, MAX_RETRIES)
                await asyncio.sleep(delay)
                continue
            raise LLMError(
                "The LLM provider timed out. Some free tiers queue requests one at a time — "
                "try a smaller date range or a faster model."
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the LLM provider: {exc}") from exc

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

    raise LLMError(str(last_error) if last_error else "The LLM call failed.")


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
        return "Rate limited (429) by the LLM provider even after retries. Wait a minute or lower concurrency."
    return f"LLM provider error {status}: {body[:200]}"


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
)

_PREFERRED_ORDER = (
    "flash-lite", "flash", "gpt-oss-20b", "gpt-oss-120b",
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
