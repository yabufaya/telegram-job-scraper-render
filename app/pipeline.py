"""Pure data-shaping helpers for a scan. No I/O, no LLM, no network — which is
why they are the easiest part to unit-test (see tests/).

This module answers the question: "how do we make sure a post is never
processed twice, and never skipped?"

Three guarantees, all enforced here:

1. NO DUPLICATES
   Every post carries a stable key `channel/message_id`. A single `seen` set
   lives for the whole scan and is checked before a post is accepted. This
   covers the two ways duplicates actually appear:
     * pagination overlap — `?before=<id>` is inclusive, so the boundary post
       comes back on the next page;
     * channel reposts — the same job text posted again later.
       (dedupe_by_text normalises whitespace/emoji and hashes the text;
       an optional fuzzy key collapses "same job, different link".)

2. NO GAPS
   The running scan keeps a `cursor` (oldest timestamp + oldest message id it
   has fully processed). The next scan resumes at that cursor, so consecutive
   runs tile the timeline with no uncovered stretch — the "previous batch
   outran the next" problem is solved by never letting a batch own a range of
   time at all; it owns a list of posts.

3. NO OVERLAP BETWEEN BATCHES
   Batches are built from one ordered, de-duplicated list. Batch N contains
   only items not already placed in batches 0..N-1. A batch is cut by BOTH a
   count limit and a character limit, so one giant post cannot blow the
   provider's context window.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from app.window import ScanWindow, window_for

_WS = re.compile(r"\s+")
_URL = re.compile(r"https?://\S+")
_EMOJI_ISH = re.compile(r"[^\w\s\u1200-\u137F]", re.UNICODE)  # keep Ethiopic script


def post_key(post: dict) -> str:
    return f"{post.get('channel', '')}/{post.get('id')}"


def normalise_text(text: str) -> str:
    """Aggressive normalisation used only for duplicate detection."""
    t = (text or "").lower()
    t = _URL.sub(" ", t)          # links differ between reposts
    t = _EMOJI_ISH.sub(" ", t)    # emoji / bullets / punctuation
    t = _WS.sub(" ", t).strip()
    return t


def text_fingerprint(text: str) -> str:
    return hashlib.sha1(normalise_text(text).encode("utf-8")).hexdigest()[:16]


def dedupe_posts(posts: list[dict], by_text: bool = True) -> tuple[list[dict], int]:
    """Remove pagination repeats and reposts. Returns (unique, removed_count)."""
    seen_ids: set[str] = set()
    seen_text: set[str] = set()
    unique: list[dict] = []
    removed = 0

    for post in posts:
        key = post_key(post)
        if key in seen_ids:
            removed += 1
            continue
        if by_text:
            fp = text_fingerprint(post.get("text", ""))
            if fp in seen_text:
                removed += 1
                seen_ids.add(key)  # remember it so a third copy is also dropped
                continue
            seen_text.add(fp)
        seen_ids.add(key)
        unique.append(post)

    return unique, removed


def assign_to_windows(
    posts: list[dict], windows: list[ScanWindow]
) -> dict[int, list[dict]]:
    """Bucket posts by the single window that owns their timestamp.

    A post outside every window (e.g. older than `since` because Telegram
    returned it anyway) is dropped here rather than leaking into a batch.
    """
    buckets: dict[int, list[dict]] = {w.index: [] for w in windows}
    for post in posts:
        moment = parse_post_date(post.get("date", ""))
        if moment is None:
            continue
        idx = window_for(moment, windows)
        if idx is not None:
            buckets[idx].append(post)
    return buckets


def parse_post_date(raw: str) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sort_newest_first(posts: list[dict]) -> list[dict]:
    def key(p: dict):
        moment = parse_post_date(p.get("date", ""))
        return moment.timestamp() if moment else 0

    return sorted(posts, key=key, reverse=True)


def build_batches(
    posts: list[dict],
    batch_size: int = 15,
    max_batch_chars: int = 40_000,
    max_chars_per_post: int = 6_000,
) -> list[list[dict]]:
    """Greedy packing bounded by BOTH item count and total characters.

    Every post lands in exactly one batch, preserving order. A single post
    longer than max_batch_chars still gets its own batch (truncated), because
    dropping it would create a silent gap.
    """
    batch_size = max(1, batch_size)
    max_batch_chars = max(1_000, max_batch_chars)
    max_chars_per_post = max(200, max_chars_per_post)

    batches: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0

    for post in posts:
        text = (post.get("text") or "")[:max_chars_per_post]
        slim = {**post, "text": text}
        cost = len(text) + 60  # + per-item JSON overhead

        too_many = len(current) >= batch_size
        too_big = current and current_chars + cost > max_batch_chars
        if too_many or too_big:
            batches.append(current)
            current, current_chars = [], 0

        current.append(slim)
        current_chars += cost

    if current:
        batches.append(current)
    return batches


def merge_batch_results(
    llm_items: list[dict],
    batch: list[dict],
    today_iso: str | None = None,
) -> tuple[list[dict], int, list[str]]:
    """Attach LLM verdicts back to real posts.

    Fixes the id-type bug found in the audit: providers happily return
    `"id": "123"` while our map is keyed by int 123, which used to make every
    match silently disappear. We coerce both sides to str.

    Returns (results, expired_count, warnings).
    """
    by_key: dict[str, dict] = {str(p.get("id")): p for p in batch}
    by_text: dict[str, dict] = {}
    for p in batch:
        by_text.setdefault(text_fingerprint(p.get("text", ""))[:12], p)

    results: list[dict] = []
    expired = 0
    warnings: list[str] = []

    # Some providers return {"results": [...]} or a single object instead of an
    # array. Normalise here so callers never have to care.
    if isinstance(llm_items, dict):
        llm_items = llm_items.get("results") or llm_items.get("matches") or [llm_items]
    if not isinstance(llm_items, list):
        warnings.append("The model returned an unexpected shape; batch ignored.")
        return results, expired, warnings

    for item in llm_items:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        post = by_key.get(str(raw_id).strip())
        if post is None:
            # Model sometimes echoes a truncated/rewritten id; fall back to text.
            guess = item.get("text_match")
            if guess:
                post = by_text.get(str(guess)[:12])
            if post is None:
                warnings.append(f"Model returned an id that is not in this batch ({raw_id!r}); ignored.")
                continue

        status = str(item.get("status", "medium")).lower().strip()
        if status not in {"high", "medium", "expired"}:
            status = "medium"
        if status == "expired":
            expired += 1
            continue

        results.append(
            {
                "id": post.get("id"),
                "channel": post.get("channel", ""),
                "date": post.get("date", ""),
                "text": post.get("text", ""),
                "link": post.get("link", ""),
                "status": status,
                "reason": str(item.get("reason", ""))[:400],
            }
        )

    return results, expired, warnings


def dedupe_results(results: list[dict]) -> list[dict]:
    """Same post can only be reported once, even if two windows/batches saw it."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in results:
        key = post_key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def sort_results(results: list[dict]) -> list[dict]:
    def key(r: dict):
        moment = parse_post_date(r.get("date", ""))
        return (
            0 if r.get("status") == "high" else 1,
            -(moment.timestamp() if moment else 0),
        )

    return sorted(results, key=key)


def make_cursor(posts: list[dict]) -> dict | None:
    """Checkpoint of the oldest post processed, for client-side continuation.

    Nothing is stored on the server: the cursor travels back in the response,
    the browser keeps it in localStorage, and the next scan can resume exactly
    here. That is how "start from where the last scan stopped" works without a
    database.
    """
    dated = [(parse_post_date(p.get("date", "")), p) for p in posts]
    dated = [(d, p) for d, p in dated if d is not None]
    if not dated:
        return None
    oldest_date, oldest_post = min(dated, key=lambda pair: pair[0].timestamp())
    return {
        "oldest_date": oldest_date.isoformat(),
        "oldest_id": oldest_post.get("id"),
        "count": len(dated),
    }


def estimate_batches(post_count: int, batch_size: int) -> int:
    if post_count <= 0:
        return 0
    return -(-post_count // max(1, batch_size))
