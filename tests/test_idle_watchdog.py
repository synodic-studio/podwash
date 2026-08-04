"""Trip logic for the Mac-side idle watchdog.

We only unit-test `evaluate()` and the cooldown stamp helper here —
the full main() flow shells out to subprocess and `python -m src.heal`,
which is exercised end-to-end by the wrapper tests.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.worker import idle_watchdog


def _snapshot(
    *, pending: int = 0, in_flight: int = 0, last_claim_at: str | None = None
) -> dict:
    return {
        "pending_count": pending,
        "in_flight_count": in_flight,
        "last_claim_at": last_claim_at,
        "last_claim_by": "test-worker",
        "oldest_pending_id": 1 if pending else None,
    }


def test_evaluate_quiet_when_queue_empty() -> None:
    should, reason = idle_watchdog.evaluate(_snapshot(), no_claim_minutes=30)
    assert should is False
    assert "empty" in reason


def test_evaluate_quiet_when_recent_claim() -> None:
    now = datetime(2026, 4, 27, 12, 0, 0)
    recent = (now - timedelta(minutes=5)).isoformat()
    should, reason = idle_watchdog.evaluate(
        _snapshot(pending=3, last_claim_at=recent),
        no_claim_minutes=30,
        now=now,
    )
    assert should is False
    assert "under" in reason


def test_evaluate_trips_when_silent_past_threshold() -> None:
    now = datetime(2026, 4, 27, 12, 0, 0)
    old = (now - timedelta(hours=2)).isoformat()
    should, reason = idle_watchdog.evaluate(
        _snapshot(pending=2, last_claim_at=old),
        no_claim_minutes=30,
        now=now,
    )
    assert should is True
    assert "120" in reason


def test_evaluate_trips_when_no_claim_ever() -> None:
    should, reason = idle_watchdog.evaluate(
        _snapshot(pending=1, last_claim_at=None), no_claim_minutes=30
    )
    assert should is True
    assert "never seen a claim" in reason


def test_cooldown_stamp_blocks_repeat_fire(tmp_path: Path) -> None:
    stamp = tmp_path / "last-heal.stamp"
    assert idle_watchdog._within_cooldown(stamp, 1800) is False
    idle_watchdog._stamp_now(stamp)
    assert idle_watchdog._within_cooldown(stamp, 1800) is True
    # And after the cooldown expires we can fire again.
    stamp.write_text(f"{time.time() - 2000:.0f}\n")
    assert idle_watchdog._within_cooldown(stamp, 1800) is False


def test_cooldown_stamp_handles_corrupt_file(tmp_path: Path) -> None:
    stamp = tmp_path / "last-heal.stamp"
    stamp.write_text("not a number\n")
    # Garbage in → safe default (allow firing).
    assert idle_watchdog._within_cooldown(stamp, 1800) is False


def test_first_trip_does_not_confirm(tmp_path: Path) -> None:
    trip = tmp_path / "last-trip.json"
    snap = _snapshot(pending=1)
    assert (
        idle_watchdog._confirm_repeat_trip(trip, snap, window_seconds=1200) is False
    )


def test_second_trip_on_same_backlog_confirms(tmp_path: Path) -> None:
    trip = tmp_path / "last-trip.json"
    snap = _snapshot(pending=1)
    idle_watchdog._confirm_repeat_trip(trip, snap, window_seconds=1200)
    assert (
        idle_watchdog._confirm_repeat_trip(trip, snap, window_seconds=1200) is True
    )


def test_queue_moving_on_restarts_the_streak(tmp_path: Path) -> None:
    trip = tmp_path / "last-trip.json"
    first = _snapshot(pending=1)
    idle_watchdog._confirm_repeat_trip(trip, first, window_seconds=1200)
    moved = dict(first, oldest_pending_id=99)
    assert (
        idle_watchdog._confirm_repeat_trip(trip, moved, window_seconds=1200) is False
    )


def test_stale_trip_record_does_not_confirm(tmp_path: Path) -> None:
    trip = tmp_path / "last-trip.json"
    snap = _snapshot(pending=1)
    now = time.time()
    idle_watchdog._confirm_repeat_trip(trip, snap, window_seconds=1200, now=now)
    assert (
        idle_watchdog._confirm_repeat_trip(
            trip, snap, window_seconds=1200, now=now + 1201
        )
        is False
    )


def test_corrupt_trip_record_does_not_confirm(tmp_path: Path) -> None:
    trip = tmp_path / "last-trip.json"
    trip.write_text("not json")
    assert (
        idle_watchdog._confirm_repeat_trip(
            trip, _snapshot(pending=1), window_seconds=1200
        )
        is False
    )


def test_evaluate_handles_aware_last_claim_with_naive_now() -> None:
    now = datetime(2026, 4, 27, 12, 0, 0)  # naive
    old = "2026-04-27T10:00:00+00:00"  # aware
    should, _ = idle_watchdog.evaluate(
        _snapshot(pending=1, last_claim_at=old),
        no_claim_minutes=30,
        now=now,
    )
    assert should is True


def test_evaluate_handles_aware_last_claim_with_aware_now() -> None:
    now = datetime(2026, 4, 27, 12, 0, 0, tzinfo=timezone.utc)
    old = "2026-04-27T10:00:00+00:00"
    should, _ = idle_watchdog.evaluate(
        _snapshot(pending=1, last_claim_at=old),
        no_claim_minutes=30,
        now=now,
    )
    assert should is True
