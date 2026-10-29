"""Scan-window logic: where a scan starts, how far back it may reach, and how
the date range is cut into contiguous, non-overlapping windows.

WHY WINDOWS AT ALL
------------------
A user types "since 2026-09-01". That is not one unit of work — it can be 40
days x 30 posts = 1,200 posts. We cut [since, now) into fixed windows
(default 14 days). Each window:

  * is exactly one slice of time:  [t0, t1)  [t1, t2)  [t2, now)
  * owns every post whose timestamp falls inside it — a post can be in at
    most one window, so windows can never overlap by construction
  * is scanned and classified on its own, so a failure in one window does not
    silently corrupt another, and a retry only redoes THAT window
  * can be reported to the user as "window 2/3 done", which is meaningful
    progress instead of a spinner

The half-open interval `[start, end)` is the important detail: a post at
exactly t1 belongs to the second window only, never to both.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

DEFAULT_WINDOW_DAYS = 14


class WindowError(ValueError):
    """User-fixable problem with the requested date range."""


@dataclass(frozen=True)
class ScanWindow:
    index: int
    start: datetime           # inclusive, UTC
    end: datetime             # exclusive, UTC
    label: str

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end


def parse_since(raw: str, tz_offset_minutes: int = 0) -> datetime:
    """Turn the UI's `since` value into an aware UTC datetime.

    `raw` is usually a plain date ("2026-09-01"). The browser also sends
    `tz_offset_minutes` from `new Date().getTimezoneOffset()` so that "since
    Sept 1" means *the user's* Sept 1 midnight, not UTC midnight — otherwise a
    post published 21:00 UTC on Aug 31 (= Sept 1, 00:00 in Addis) is wrongly
    excluded.

    A full ISO datetime is also accepted and respected as-is.
    """
    text = (raw or "").strip()
    if not text:
        raise WindowError("A 'since' date is required")

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WindowError("'since' must be a date like 2026-09-01") from exc

    if parsed.tzinfo is None:
        # Local midnight of that date -> UTC.
        try:
            offset = int(tz_offset_minutes)
        except (TypeError, ValueError):
            offset = 0
        # getTimezoneOffset() convention: UTC = local + offset
        parsed = parsed.replace(tzinfo=timezone.utc) + timedelta(minutes=offset)

    since = parsed.astimezone(timezone.utc).replace(microsecond=0)
    now = datetime.now(timezone.utc)
    if since > now:
        raise WindowError("That date is in the future — pick a past date")
    return since


def clamp_lookback(since: datetime, max_lookback_days: int) -> tuple[datetime, bool]:
    """Don't let one scan crawl three years of history.

    Returns (effective_since, was_clamped). When clamped, the caller tells the
    user so they can run an earlier range as a separate scan (the UI offers a
    "scan earlier" button that reuses the returned cursor).
    """
    if max_lookback_days <= 0:
        return since, False
    floor = datetime.now(timezone.utc) - timedelta(days=max_lookback_days)
    if since < floor:
        return floor, True
    return since, False


def plan_windows(
    since: datetime,
    until: datetime | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> list[ScanWindow]:
    """Cut [since, until) into contiguous half-open windows, newest last."""
    until = until or datetime.now(timezone.utc)
    if until <= since:
        raise WindowError("The date range is empty")

    window_days = max(1, window_days)
    span = until - since
    count = max(1, math.ceil(span / timedelta(days=window_days)))

    windows: list[ScanWindow] = []
    cursor = since
    for i in range(count):
        end = min(cursor + timedelta(days=window_days), until)
        windows.append(
            ScanWindow(
                index=i,
                start=cursor,
                end=end,
                label=f"{cursor:%d %b} → {end:%d %b}",
            )
        )
        cursor = end
        if cursor >= until:
            break
    return windows


def window_for(moment: datetime, windows: list[ScanWindow]) -> int | None:
    """Index of the window that owns this timestamp (None if outside range)."""
    for w in windows:
        if w.contains(moment):
            return w.index
    return None


def windows_summary(windows: list[ScanWindow]) -> list[dict]:
    return [
        {"index": w.index + 1, "label": w.label, "start": w.start.isoformat(), "end": w.end.isoformat()}
        for w in windows
    ]


def human_span(delta: timedelta) -> str:
    days = delta.days
    if days >= 1:
        return f"{days} day{'s' if days != 1 else ''}"
    hours = max(1, int(delta.total_seconds() // 3600))
    return f"{hours} hour{'s' if hours != 1 else ''}"


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
