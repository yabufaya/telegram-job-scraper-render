"""Unit tests for the pure logic: windows, dedupe, batching, merge.

Run:  python -m pytest -q        (or: python -m unittest discover -s tests)
These need no network, no API keys and no Telegram account.
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import pipeline  # noqa: E402
from app.window import (  # noqa: E402
    clamp_lookback,
    parse_since,
    plan_windows,
    window_for,
)

UTC = timezone.utc


def post(pid: int, iso: str, text: str = "Job post", channel: str = "ch") -> dict:
    return {"id": pid, "channel": channel, "date": iso, "text": text,
            "link": f"https://t.me/{channel}/{pid}"}


class WindowTests(unittest.TestCase):
    def test_windows_are_contiguous_and_non_overlapping(self):
        since = datetime(2026, 1, 1, tzinfo=UTC)
        until = datetime(2026, 2, 10, tzinfo=UTC)
        windows = plan_windows(since, until, window_days=14)

        self.assertEqual(windows[0].start, since)
        self.assertEqual(windows[-1].end, until)
        for a, b in zip(windows, windows[1:]):
            self.assertEqual(a.end, b.start, "a gap or overlap exists between windows")

    def test_half_open_boundary_belongs_to_one_window_only(self):
        since = datetime(2026, 1, 1, tzinfo=UTC)
        windows = plan_windows(since, since + timedelta(days=28), window_days=14)

        boundary = windows[0].end  # == start of window 1
        self.assertIsNone(window_for(boundary, [windows[0]]))
        self.assertEqual(window_for(boundary, windows), 1)
        owners = [w.index for w in windows if w.contains(boundary)]
        self.assertEqual(owners, [1], "boundary timestamps must have exactly one owner")

    def test_every_second_of_the_range_is_covered(self):
        since = datetime(2026, 3, 1, tzinfo=UTC)
        until = datetime(2026, 4, 20, 7, 30, tzinfo=UTC)
        windows = plan_windows(since, until, window_days=14)
        cursor = since
        for w in windows:
            self.assertEqual(w.start, cursor)
            cursor = w.end
        self.assertEqual(cursor, until)

    def test_since_uses_client_timezone(self):
        # Addis Ababa = UTC+3 -> getTimezoneOffset() = -180
        got = parse_since("2026-09-01", tz_offset_minutes=-180)
        self.assertEqual(got, datetime(2026, 8, 31, 21, 0, tzinfo=UTC))

    def test_since_rejects_future_and_garbage(self):
        with self.assertRaises(Exception):
            parse_since("2999-01-01", 0)
        with self.assertRaises(Exception):
            parse_since("not-a-date", 0)

    def test_lookback_clamp(self):
        old = datetime.now(UTC) - timedelta(days=999)
        clamped, did = clamp_lookback(old, 120)
        self.assertTrue(did)
        self.assertGreater(clamped, old)


class DedupeTests(unittest.TestCase):
    def test_pagination_overlap_removed(self):
        # Distinct texts, so only the repeated message id can be deduped.
        posts = [post(5, "2026-01-05T00:00:00+00:00", text="Accountant at A"),
                 post(4, "2026-01-04T00:00:00+00:00", text="Driver at B"),
                 post(4, "2026-01-04T00:00:00+00:00", text="Driver at B", channel="ch")]
        unique, removed = pipeline.dedupe_posts(posts)
        self.assertEqual(removed, 1)
        self.assertEqual([p["id"] for p in unique], [5, 4])

    def test_repost_same_text_different_id_removed(self):
        text = "Senior Accountant at ABC plc. Apply before Sept 30."
        posts = [post(5, "2026-01-05T00:00:00+00:00", text=text),
                 post(9, "2026-01-06T00:00:00+00:00", text=text + "  ")]
        unique, removed = pipeline.dedupe_posts(posts, by_text=True)
        self.assertEqual(removed, 1)
        self.assertEqual(len(unique), 1)

    def test_repost_kept_when_text_dedupe_disabled(self):
        text = "Same job text"
        posts = [post(5, "2026-01-05T00:00:00+00:00", text=text),
                 post(9, "2026-01-06T00:00:00+00:00", text=text)]
        unique, removed = pipeline.dedupe_posts(posts, by_text=False)
        self.assertEqual(removed, 0)
        self.assertEqual(len(unique), 2)

    def test_different_jobs_same_words_not_collapsed(self):
        a = post(1, "2026-01-01T00:00:00+00:00", text="Electrician at Company A. Deadline Sept 30.")
        b = post(2, "2026-01-02T00:00:00+00:00", text="Electrician at Company B. Deadline Oct 5.")
        unique, removed = pipeline.dedupe_posts([a, b], by_text=True)
        self.assertEqual(removed, 0)
        self.assertEqual(len(unique), 2)


class BatchTests(unittest.TestCase):
    def test_batches_partition_the_list_exactly_once(self):
        posts = [post(i, "2026-01-01T00:00:00+00:00", text=f"job {i}") for i in range(37)]
        batches = pipeline.build_batches(posts, batch_size=15, max_batch_chars=1_000_000)
        flat = [p["id"] for b in batches for p in b]
        self.assertEqual(flat, [p["id"] for p in posts], "order and coverage must be preserved")
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b) for b in batches], [15, 15, 7])

    def test_char_limit_splits_before_count_limit(self):
        posts = [post(i, "2026-01-01T00:00:00+00:00", text="x" * 5_000) for i in range(6)]
        batches = pipeline.build_batches(posts, batch_size=50, max_batch_chars=12_000,
                                         max_chars_per_post=6_000)
        self.assertGreater(len(batches), 1)
        self.assertEqual(sum(len(b) for b in batches), 6)

    def test_giant_post_gets_its_own_batch_not_dropped(self):
        posts = [post(1, "2026-01-01T00:00:00+00:00", text="y" * 9_000),
                 post(2, "2026-01-02T00:00:00+00:00", text="small")]
        batches = pipeline.build_batches(posts, batch_size=15, max_batch_chars=2_000,
                                         max_chars_per_post=6_000)
        ids = [p["id"] for b in batches for p in b]
        self.assertIn(1, ids)
        self.assertIn(2, ids)
        self.assertLessEqual(len(batches[0][0]["text"]), 6_000)

    def test_no_batch_contains_a_post_twice(self):
        posts = [post(i, "2026-01-01T00:00:00+00:00") for i in range(50)]
        batches = pipeline.build_batches(posts, batch_size=7, max_batch_chars=100_000)
        ids = [p["id"] for b in batches for p in b]
        self.assertEqual(len(ids), len(set(ids)))


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.batch = [post(101, "2026-01-01T00:00:00+00:00", text="Engineer job A"),
                      post(102, "2026-01-02T00:00:00+00:00", text="Manager job B")]

    def test_string_ids_from_model_are_matched(self):
        results, expired, warns = pipeline.merge_batch_results(
            [{"id": "101", "status": "high", "reason": "fits"}], self.batch
        )
        self.assertEqual(len(results), 1, "string id must match int id (audit bug #1)")
        self.assertEqual(results[0]["id"], 101)
        self.assertEqual(warns, [])

    def test_expired_counted_and_excluded_from_results(self):
        results, expired, _ = pipeline.merge_batch_results(
            [{"id": 102, "status": "expired", "reason": "Deadline was Sept 1"}], self.batch
        )
        self.assertEqual(results, [])
        self.assertEqual(expired, 1)

    def test_unknown_id_warns_instead_of_crashing(self):
        results, _, warns = pipeline.merge_batch_results(
            [{"id": 999, "status": "high", "reason": "x"}], self.batch
        )
        self.assertEqual(results, [])
        self.assertEqual(len(warns), 1)

    def test_object_instead_of_array_is_handled(self):
        results, expired, _ = pipeline.merge_batch_results(
            {"results": [{"id": 101, "status": "medium", "reason": "ok"}]}, self.batch
        )
        self.assertEqual(len(results), 1)

    def test_bad_status_defaults_to_medium(self):
        results, _, _ = pipeline.merge_batch_results(
            [{"id": 101, "status": "definitely", "reason": "x"}], self.batch
        )
        self.assertEqual(results[0]["status"], "medium")


class CursorTests(unittest.TestCase):
    def test_cursor_is_oldest_post(self):
        posts = [post(3, "2026-01-03T00:00:00+00:00"), post(1, "2026-01-01T00:00:00+00:00"),
                 post(2, "2026-01-02T00:00:00+00:00")]
        cursor = pipeline.make_cursor(posts)
        self.assertEqual(cursor["oldest_id"], 1)
        self.assertTrue(cursor["oldest_date"].startswith("2026-01-01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
