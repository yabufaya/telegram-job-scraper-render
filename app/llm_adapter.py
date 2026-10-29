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

SYSTEM_PROMPT = """You are a job-post relevance filter. You will receive:
1. Today's date, for checking whether a post's deadline has passed.
2. A user's search criteria (their background, target field, and requirements).
3. A batch of Telegram channel posts, each with an id, date, and text.

For each post, first check if it has an application deadline stated in the
text (look for words like "Deadline", "Apply before", "Closing date", or a
date near the top/bottom of the post).

- If a deadline is stated AND that deadline is before today's date, the
  post is EXPIRED. Do not treat it as a match, regardless of how well it
  fits the user's criteria.
- If a deadline is stated and it is today or in the future, treat it
  normally alongside the matching rules below.
- If no deadline is stated at all, treat it normally — do not assume it's
  expired just because no date is given.

For every post that is NOT expired, decide whether it is a genuine job
posting that could reasonably match the user's criteria.

SENIORITY CHECK (do this explicitly, separately from field matching):
- First identify each position's seniority level from its title and any
  stated years-of-experience requirement. Titles containing "Senior",
  "Manager", "Lead", "Expert", "Head", "Officer" (when paired with a
  years requirement), or an explicit "X+ years" are NOT entry-level,
  even if the field/degree matches perfectly.
- Titles containing "Junior", "Trainee", "Intern", "Graduate Trainee",
  "Associate", or with no experience stated, ARE entry-level.
- If the user's criteria specifies an entry-level / fresh-graduate / 0
  years profile, a post whose relevant position(s) are all senior-level
  is NOT a match — do not include it just because the field/degree
  overlaps. A perfect field match at the wrong seniority is still not a
  match. Only include it if the post explicitly also lists a
  junior/entry-level position in the same relevant field.
- If the user's criteria specifies a senior/experienced profile, apply
  the same logic in reverse — a junior-only post is not a match for them.
- If the post lists multiple positions in one message (common in these
  channels), evaluate them individually: match if AT LEAST ONE listed
  position in the relevant field also fits the user's seniority level.

FIELD CHECK (only after the seniority check passes):
- Match on MEANING, not exact wording. Treat phrases like "fresh graduate",
  "entry level", "no experience required", "new grads welcome", "0 exp" as
  equivalent to "0 years experience."
- Posts may use abbreviations, emojis, or bullet-point formatting — parse
  around this, don't require clean prose.
- If a post is ambiguous or partially matches, INCLUDE it with status
  "medium" rather than excluding it. Never silently drop a plausible match.
- Ignore posts that are clearly not job postings (ads, announcements, event
  promos) even if they share keywords.
- Do not invent information not present in the post text.

Return ONLY a JSON array, one object per post that is either a plausible
match OR expired (so the caller can report how many were filtered out), in
this exact shape:
[
  {"id": <post id, same type as given>, "status": "high" | "medium" | "expired", "reason": "<one short sentence>"}
]
For expired posts, set "status" to "expired" and the reason to the deadline
found, e.g. "Deadline was Sept 25, 2026 — expired."
Use the id EXACTLY as provided (do not change its type or formatting).
Omit posts that are neither a plausible match nor expired. Do not include any text outside the JSON array."""

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
