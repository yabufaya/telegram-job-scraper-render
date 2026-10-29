"""Two ways to read a public Telegram channel, both stateless.

SOURCE A — public web preview  (t.me/s/<channel>)
    No account. No API id/hash. No session file. Cannot get anyone banned.
    ~80% of job channels have it enabled. Paginated with ?before=<message_id>
    (inclusive, so we always drop the boundary post explicitly).

SOURCE B — Telethon fallback (only if TELEGRAM_SESSION_STRING is configured)
    Used when a channel has the preview switched off. Uses a StringSession kept
    in an environment variable, never a file on disk — Render's filesystem is
    wiped on every deploy and every idle spin-down.

Both return the same shape: {id, channel, date, text, link}.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.window import ScanWindow

logger = logging.getLogger("job_filter")

PREVIEW_BASE = "https://t.me/s/"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class SourceError(Exception):
    """Any failure that should be shown to the user in plain language."""


class PreviewUnavailable(SourceError):
    """Channel exists but its public preview is disabled (HTTP 302 to t.me/<ch>)."""


# --------------------------------------------------------------------------- #
# SOURCE A: public preview
# --------------------------------------------------------------------------- #
def _parse_page(html: str, channel: str) -> tuple[list[dict], int | None]:
    """Return (posts, next_before_id) from one preview page."""
    soup = BeautifulSoup(html, "html.parser")
    posts: list[dict] = []
    lowest_id: int | None = None

    for node in soup.select("div.tgme_widget_message"):
        data_post = node.get("data-post") or ""
        if "/" not in data_post:
            continue
        _, _, id_part = data_post.partition("/")
        try:
            message_id = int(id_part)
        except ValueError:
            continue

        time_el = node.select_one("time[datetime]")
        if time_el is None:
            continue
        try:
            published = datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
        except ValueError:
            continue

        text_el = node.select_one("div.tgme_widget_message_text")
        text = text_el.get_text("\n", strip=True) if text_el else ""
        if not text.strip():
            continue  # media-only / service message: nothing for the LLM to read

        posts.append(
            {
                "id": message_id,
                "channel": channel,
                "date": published.astimezone(timezone.utc).isoformat(),
                "text": text,
                "link": f"https://t.me/{channel}/{message_id}",
            }
        )
        lowest_id = message_id if lowest_id is None else min(lowest_id, message_id)

    return posts, lowest_id


async def fetch_preview(
    channel: str,
    since: datetime,
    until: datetime,
    max_posts: int = 600,
    max_pages: int = 60,
) -> list[dict]:
    """Page backwards through the public preview until `since` is reached."""
    collected: list[dict] = []
    before: int | None = None
    url = f"{PREVIEW_BASE}{channel}"
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

    async with httpx.AsyncClient(timeout=25, follow_redirects=False, headers=headers) as http:
        for page in range(max_pages):
            page_url = url if before is None else f"{url}?before={before}"
            try:
                resp = await http.get(page_url)
            except httpx.HTTPError as exc:
                raise SourceError(f"Could not reach Telegram's public preview: {exc}") from exc

            if resp.status_code in (301, 302, 303, 307, 308):
                # t.me/s/<ch> redirects to t.me/<ch> when the owner disabled preview.
                raise PreviewUnavailable(
                    "This channel has its public web preview turned off, so it can only be "
                    "read through a Telegram login."
                )
            if resp.status_code == 404:
                raise SourceError(
                    f"No public channel called '{channel}'. Check the username (it must be the "
                    "@handle of a public channel, not a display name)."
                )
            if resp.status_code == 429:
                raise SourceError("Telegram is rate-limiting this server. Try again in a minute.")
            if resp.status_code >= 400:
                raise SourceError(f"Telegram preview returned HTTP {resp.status_code}.")

            posts, lowest_id = _parse_page(resp.text, channel)
            if not posts:
                break  # no more history

            # Keep only posts inside [since, until)
            in_range = []
            for post in posts:
                published = datetime.fromisoformat(post["date"])
                if published >= until:
                    continue  # newer than the requested range
                if published < since:
                    continue
                in_range.append(post)
            collected.extend(in_range)

            oldest_on_page = min(datetime.fromisoformat(p["date"]) for p in posts)
            if len(collected) >= max_posts:
                logger.info("Preview cap reached (%s posts).", max_posts)
                break
            if oldest_on_page < since:
                break  # we have walked past the requested start
            if lowest_id is None:
                break

            # ?before= is inclusive: the next page repeats this post, which
            # dedupe_posts() removes. We step by 1 to be explicit about it.
            next_before = lowest_id
            if next_before == before or next_before <= 1:
                break
            before = next_before
            await asyncio.sleep(0.35)  # be a polite citizen

    return collected[:max_posts]


# --------------------------------------------------------------------------- #
# SOURCE B: Telethon fallback (lazy, never crashes at import)
# --------------------------------------------------------------------------- #
_telethon_client = None
_telethon_lock = asyncio.Lock()


async def get_telethon_client():
    """Create/connect the shared Telethon client on first use.

    Returns None when the deployment has no valid session string — the caller
    then reports a friendly message instead of the app failing to boot.
    """
    global _telethon_client

    if not settings.telethon_ready:
        return None

    async with _telethon_lock:
        if _telethon_client is not None:
            return _telethon_client

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(settings.tg_session_string),
            settings.tg_api_id,
            settings.tg_api_hash,
            connection_retries=2,
            timeout=20,
        )
        await client.connect()
        if not await client.is_user_authorized():
            logger.error("TELEGRAM_SESSION_STRING is set but not authorised (revoked or corrupt).")
            await client.disconnect()
            return None

        logger.info("Telethon fallback connected (StringSession).")
        _telethon_client = client
        return client


async def fetch_telethon(
    channel: str, since: datetime, until: datetime, max_posts: int = 600
) -> list[dict]:
    client = await get_telethon_client()
    if client is None:
        raise SourceError(
            "This channel needs a Telegram login, and this deployment has no working "
            "TELEGRAM_SESSION_STRING configured."
        )

    from telethon.errors import FloodWaitError

    try:
        entity = await client.get_entity(channel)
    except Exception as exc:  # noqa: BLE001 - Telethon raises many types
        raise SourceError(f"Could not open channel '{channel}': {exc}") from exc

    posts: list[dict] = []
    try:
        async for message in client.iter_messages(entity, limit=max_posts, offset_date=until):
            published = message.date.astimezone(timezone.utc)
            if published >= until:
                continue
            if published < since:
                break  # newest-first, so everything older is out of range too
            if not message.text:
                continue
            posts.append(
                {
                    "id": message.id,
                    "channel": channel,
                    "date": published.isoformat(),
                    "text": message.text,
                    "link": f"https://t.me/{channel}/{message.id}",
                }
            )
    except FloodWaitError as exc:
        raise SourceError(
            f"Telegram asked this server to slow down for {exc.seconds}s. Try again later."
        ) from exc

    return posts


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
async def fetch_window(
    channel: str,
    window: ScanWindow,
    max_posts: int,
    want_telethon_first: bool = False,
) -> tuple[list[dict], str]:
    """Fetch one window, honouring the configured source order.

    Returns (posts, source_used). `source_used` is shown in the UI so users can
    see whether they got the safe no-login route or the fallback.
    """
    order = list(settings.tg_source_order)
    if want_telethon_first:
        order.sort(key=lambda s: 0 if s == "telethon" else 1)

    last_error: SourceError | None = None
    for source in order:
        try:
            if source == "preview":
                posts = await fetch_preview(channel, window.start, window.end, max_posts=max_posts)
                logger.info("Window %s: preview returned %s post(s).", window.label, len(posts))
                return posts, "preview"

            if source == "telethon":
                if not settings.telethon_ready:
                    continue
                posts = await fetch_telethon(channel, window.start, window.end, max_posts=max_posts)
                logger.info("Window %s: telethon returned %s post(s).", window.label, len(posts))
                return posts, "telethon"
        except PreviewUnavailable as exc:
            logger.info("Window %s: preview disabled for %s, trying next source.", window.label, channel)
            last_error = exc
            continue
        except SourceError as exc:
            logger.warning("Window %s: source %s failed: %s", window.label, source, exc)
            last_error = exc
            continue

    raise last_error or SourceError("No usable Telegram source is configured for this channel.")
