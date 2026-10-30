"""Input validation + abuse guards.

- LLM base URLs are user-supplied, so they are an SSRF vector on a public host:
  we force https:// and refuse loopback/private/link-local/metadata IPs.
- A small in-memory rate limiter keeps one visitor (or a script) from turning
  your Render instance into a free scraping proxy.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from collections import deque
from urllib.parse import urlparse

ALLOWED_LLM_HOSTS_SUFFIXES = (
    # well-known OpenAI-compatible providers; anything else is allowed too,
    # this list only exists to document intent.
)

# Hosts that must never be reachable from the server.
BLOCKED_HOSTNAMES = {
    "localhost",
    "metadata",
    "metadata.google.internal",
    "instance-data",
    "169.254.169.254",
}


class ValidationError(ValueError):
    """Raised for user-fixable input problems (surfaced as HTTP 400)."""


def validate_llm_base_url(url: str, allow_insecure: bool = False) -> str:
    """Return a normalised base URL or raise ValidationError.

    Rules:
      * https:// required (http only if ALLOW_INSECURE_LLM_URLS=true, for LAN testing)
      * no credentials embedded in the URL
      * hostname must not resolve to a private / loopback / link-local / reserved IP
    """
    url = (url or "").strip()
    if not url:
        raise ValidationError("LLM base URL is required, e.g. https://generativelanguage.googleapis.com/v1beta/openai")

    if "://" not in url:
        url = "https://" + url

    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in {"https", "http"}:
        raise ValidationError("LLM base URL must start with https://")
    if scheme == "http" and not allow_insecure:
        raise ValidationError("LLM base URL must use https:// (http is only allowed for local testing)")

    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValidationError("LLM base URL has no hostname")
    if parsed.username or parsed.password:
        raise ValidationError("Do not put credentials inside the URL — use the API key field")

    if host in BLOCKED_HOSTNAMES:
        raise ValidationError("That hostname is not allowed")

    # Resolve and reject internal address ranges. Fail closed on resolution error.
    # When ALLOW_INSECURE_LLM_URLS is on (local testing only) we also permit
    # loopback/private targets so a developer can point at a mock server.
    if allow_insecure:
        return url.rstrip("/")

    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValidationError(f"Could not resolve host '{host}'") from exc

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValidationError("That URL points to a private/internal address and is blocked")

    return url.rstrip("/")


def validate_channel(raw: str) -> str:
    """Accept '@name', 'name', 'https://t.me/name' or 't.me/s/name'.

    Rejects invite links and message links with a message that says what to do
    instead. Both used to slip through this function's regex: "t.me/+abc" and
    "t.me/joinchat/xxx" silently produced a username fragment, and a message
    link such as t.me/channel/22374 produced the channel "22374".
    """
    import re

    text = (raw or "").strip().strip("<>()[]\"'")
    if not text:
        raise ValidationError("Channel is required")

    low = text.lower()
    if "joinchat" in low or re.search(r"t\.me/\+", low) or low.startswith("+") or "t.me/addstickers" in low:
        raise ValidationError(
            "Private invite links are not supported. Use the public @username of the channel."
        )

    # Pull out the path portion of a t.me link, if it is one.
    m = re.search(r"(?:https?://)?t\.me/(?:s/)?([^\s?#]+)", text, re.I)
    if m:
        path = m.group(1).strip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            raise ValidationError(
                f"'{text}' is a link to a single message. Use just the channel: "
                f"@{parts[0] or 'channelname'}"
            )
        candidate = parts[0] if parts else ""
    else:
        candidate = text.lstrip("@").strip("/")

    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", candidate):
        raise ValidationError(
            "That doesn't look like a public channel. Use a username like @somechannel "
            "or a link like https://t.me/somechannel"
        )
    if candidate.lower() in {"joinchat", "addstickers", "share", "proxy"}:
        raise ValidationError("Private invite links are not supported — use a public channel username")
    if len(candidate) < 5:
        raise ValidationError(f"@{candidate} is too short — Telegram usernames are at least 5 characters")
    return candidate


MAX_CHANNELS = 6


def parse_channels(raw: str) -> list[str]:
    """Accept one channel or many: newlines, commas, spaces, @handles or links.

    Returns a de-duplicated, order-preserving list. Raises ValidationError with
    a message naming the offending entry, so the visitor knows which one is bad.
    """
    import re

    text = (raw or "").strip()
    if not text:
        raise ValidationError("Enter at least one channel")

    # Split on newlines, commas, semicolons and whitespace.
    pieces = [p for p in re.split(r"[\s,;]+", text) if p.strip()]
    if not pieces:
        raise ValidationError("Enter at least one channel")

    # Collapse the old two-box shape: "https://t.me/x https://t.me/y".
    seen: set[str] = set()
    channels: list[str] = []
    for piece in pieces:
        try:
            name = validate_channel(piece)
        except ValidationError as exc:
            raise ValidationError(f"'{piece}': {exc}") from exc
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        channels.append(name)

    if not channels:
        raise ValidationError("Enter at least one channel")
    if len(channels) > MAX_CHANNELS:
        raise ValidationError(
            f"Up to {MAX_CHANNELS} channels per scan. You sent {len(channels)} — "
            f"split them into two scans."
        )
    return channels


def validate_criteria(raw: str) -> str:
    text = (raw or "").strip()
    if len(text) < 3:
        raise ValidationError("Describe what you're looking for (at least a few words)")
    if len(text) > 2000:
        raise ValidationError("That description is too long — keep it under 2000 characters")
    return text


def validate_api_key(raw: str) -> str:
    key = (raw or "").strip()
    if not key:
        raise ValidationError("Your own LLM API key is required")
    if len(key) < 8:
        raise ValidationError("That API key looks too short")
    if len(key) > 400:
        raise ValidationError("That API key looks too long")
    if any(c in key for c in " \n\r\t"):
        raise ValidationError("The API key must not contain spaces or newlines")
    return key


def validate_model(raw: str) -> str:
    model = (raw or "").strip()
    if not model:
        raise ValidationError("Model name is required, e.g. gemini-2.5-flash or glm-4.5-flash")
    if len(model) > 120 or any(c in model for c in "\n\r\t"):
        raise ValidationError("Model name looks invalid")
    return model


class RateLimiter:
    """Sliding-window limiter, per key (IP). Memory-only, dies with the process."""

    def __init__(self, per_hour: int = 6, per_ip_concurrency: int = 1):
        self.per_hour = max(1, per_hour)
        self.per_ip_concurrency = max(1, per_ip_concurrency)
        self._hits: dict[str, deque[float]] = {}
        self._inflight: dict[str, int] = {}
        self._lock = threading.Lock()

    def check_and_reserve(self, key: str) -> tuple[bool, str]:
        """Reserve a slot. Returns (ok, reason_if_not)."""
        now = time.time()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > 3600:
                hits.popleft()
            if len(hits) >= self.per_hour:
                wait = int(3600 - (now - hits[0])) + 1
                return False, f"Hourly limit reached ({self.per_hour} scans/hour). Try again in ~{wait // 60 + 1} min."
            if self._inflight.get(key, 0) >= self.per_ip_concurrency:
                return False, "You already have a scan running. Wait for it to finish."
            hits.append(now)
            self._inflight[key] = self._inflight.get(key, 0) + 1
            return True, ""

    def release(self, key: str) -> None:
        with self._lock:
            if self._inflight.get(key, 0) > 0:
                self._inflight[key] -= 1
                if self._inflight[key] == 0:
                    del self._inflight[key]


class ConcurrencyGate:
    """Global ceiling on simultaneous scans across all visitors."""

    def __init__(self, limit: int):
        self.limit = max(1, limit)
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self) -> bool:
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            if self._active > 0:
                self._active -= 1

    @property
    def active(self) -> int:
        with self._lock:
            return self._active
