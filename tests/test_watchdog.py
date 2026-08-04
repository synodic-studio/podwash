"""Tests for the server-side watchdog and the queue health snapshot it
relies on. We seed an in-memory SQLite using the production schema so
the row shapes are real, not mocked."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src import scheduler
from src.config import Settings
from src.database import db, queries


@pytest.fixture
def conn(tmp_path):
    c = db.init_db(str(tmp_path))
    queries.upsert_feed(
        c,
        type(
            "F",
            (),
            {
                "name": "f",
                "source_url": "http://x",
                "slug": "f",
                "enabled": True,
                "poll_interval_minutes": 60,
            },
        )(),
    )
    return c


_guid_counter = 0


def _insert_episode(
    conn,
    *,
    status="pending",
    claimed_at=None,
    claimed_by=None,
    failed_at=None,
    completed_at=None,
):
    global _guid_counter
    _guid_counter += 1
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, claimed_at, claimed_by,
         failed_at, completed_at)
        VALUES (1, ?, 'T', 'http://a', ?, ?, ?, ?, ?)""",
        (
            f"guid-{_guid_counter}-{status}",
            status,
            claimed_at,
            claimed_by,
            failed_at,
            completed_at,
        ),
    )
    conn.commit()
    return cur.lastrowid


def test_snapshot_empty_queue(conn):
    s = queries.queue_health_snapshot(conn)
    assert s == {
        "pending_count": 0,
        "in_flight_count": 0,
        "last_claim_at": None,
        "last_claim_by": None,
        "oldest_pending_id": None,
    }


def test_snapshot_counts_pending_and_in_flight(conn):
    _insert_episode(conn, status="pending")
    _insert_episode(conn, status="pending")
    _insert_episode(
        conn,
        status="downloading",
        claimed_at=datetime.now().isoformat(),
        claimed_by="w1",
    )
    s = queries.queue_health_snapshot(conn)
    assert s["pending_count"] == 2
    assert s["in_flight_count"] == 1
    assert s["last_claim_by"] == "w1"
    assert s["oldest_pending_id"] is not None


def test_snapshot_picks_most_recent_claim(conn):
    older = (datetime.now() - timedelta(hours=2)).isoformat()
    newer = (datetime.now() - timedelta(minutes=2)).isoformat()
    _insert_episode(conn, status="downloading", claimed_at=older, claimed_by="old")
    _insert_episode(conn, status="downloading", claimed_at=newer, claimed_by="new")
    s = queries.queue_health_snapshot(conn)
    assert s["last_claim_at"] == newer
    assert s["last_claim_by"] == "new"


def test_snapshot_reports_completion_when_nothing_in_flight(conn):
    """Completion NULLs claimed_at. A drained queue must still read as
    recently active, or every gap between episodes looks like a stall."""
    done_at = (datetime.now() - timedelta(minutes=2)).isoformat()
    _insert_episode(conn, status="completed", completed_at=done_at)
    _insert_episode(conn, status="pending")
    s = queries.queue_health_snapshot(conn)
    assert s["last_claim_at"] == done_at
    assert s["in_flight_count"] == 0


def test_snapshot_reports_failure_as_activity(conn):
    failed_at = (datetime.now() - timedelta(minutes=3)).isoformat()
    _insert_episode(conn, status="failed", failed_at=failed_at)
    s = queries.queue_health_snapshot(conn)
    assert s["last_claim_at"] == failed_at


def test_snapshot_prefers_newest_activity_of_any_kind(conn):
    old_claim = (datetime.now() - timedelta(hours=3)).isoformat()
    recent_done = (datetime.now() - timedelta(minutes=1)).isoformat()
    _insert_episode(
        conn, status="downloading", claimed_at=old_claim, claimed_by="w1"
    )
    _insert_episode(conn, status="completed", completed_at=recent_done)
    s = queries.queue_health_snapshot(conn)
    assert s["last_claim_at"] == recent_done
    assert s["last_claim_by"] == "w1"


def test_watchdog_quiet_when_queue_empty(conn, monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    assert sent == []


def test_watchdog_quiet_when_pending_but_recent_claim(conn, monkeypatch):
    _insert_episode(conn, status="pending")
    _insert_episode(
        conn,
        status="downloading",
        claimed_at=datetime.now().isoformat(),
        claimed_by="w1",
    )
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    # Recent claim → no "worker silent" alert. Stale-claim sweep also a no-op.
    assert sent == []


def test_watchdog_alerts_when_pending_with_no_recent_claim(conn, monkeypatch):
    _insert_episode(conn, status="pending")
    old_claim = (datetime.now() - timedelta(hours=2)).isoformat()
    _insert_episode(
        conn, status="completed", claimed_at=old_claim, claimed_by="dead-worker"
    )
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    assert any(
        a["subsystem"] == "server-watchdog"
        and a["kind"] == "worker silent with backlog"
        for a in sent
    )


def test_watchdog_alerts_when_no_claim_ever_recorded(conn, monkeypatch):
    _insert_episode(conn, status="pending")
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    assert sent, "expected an alert when pending exists and no worker has ever claimed"
    assert "never claimed" in sent[0]["problem"]


def test_watchdog_silently_resets_stale_claims(conn, monkeypatch):
    # Reset IS the repair. Stale claim → row flips back to 'pending'
    # for retry, no Telegram noise. (If retries hit the cap, the
    # failed-episode burst alert catches it.)
    stale = (datetime.now() - timedelta(hours=2)).isoformat()
    _insert_episode(conn, status="downloading", claimed_at=stale, claimed_by="dead")
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    settings = Settings()
    settings.worker.stale_minutes = 45
    scheduler._watchdog(conn, settings)
    assert not any(a["kind"] == "stale in-flight claims reset" for a in sent)
    row = conn.execute("SELECT status FROM episodes").fetchone()
    assert row["status"] == "pending"


def test_watchdog_alerts_on_failed_episode_burst(conn, monkeypatch):
    recent = datetime.now().isoformat()
    for _ in range(scheduler._FAILED_BURST_THRESHOLD):
        _insert_episode(conn, status="failed", failed_at=recent)
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    kinds = [a["kind"] for a in sent]
    assert "failed-episode burst" in kinds
    payload = next(a for a in sent if a["kind"] == "failed-episode burst")
    assert payload["context"]["failed_24h"] == scheduler._FAILED_BURST_THRESHOLD


def test_watchdog_quiet_when_failures_are_old(conn, monkeypatch):
    long_ago = (datetime.now() - timedelta(days=3)).isoformat()
    for _ in range(scheduler._FAILED_BURST_THRESHOLD + 2):
        _insert_episode(conn, status="failed", failed_at=long_ago)
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)
    scheduler._watchdog(conn, Settings())
    assert not any(a["kind"] == "failed-episode burst" for a in sent)


def test_count_recent_failures_respects_window(conn):
    recent = datetime.now().isoformat()
    old = (datetime.now() - timedelta(hours=48)).isoformat()
    _insert_episode(conn, status="failed", failed_at=recent)
    _insert_episode(conn, status="failed", failed_at=recent)
    _insert_episode(conn, status="failed", failed_at=old)
    # status=failed but no failed_at (legacy rows) — must be ignored.
    _insert_episode(conn, status="failed", failed_at=None)
    assert queries.count_recent_failures(conn, hours=24) == 2
    assert queries.count_recent_failures(conn, hours=72) == 3


def test_mark_failed_stamps_failed_at_only_when_terminal(conn):
    eid = _insert_episode(conn, status="downloading")
    # First failure: still has retries left, status flips to pending,
    # failed_at must remain NULL.
    queries.mark_failed(conn, eid, "boom", max_retries=3)
    row = conn.execute(
        "SELECT status, failed_at FROM episodes WHERE id=?", (eid,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["failed_at"] is None
    # Burn through remaining retries.
    queries.mark_failed(conn, eid, "boom", max_retries=3)
    queries.mark_failed(conn, eid, "boom", max_retries=3)
    row = conn.execute(
        "SELECT status, failed_at FROM episodes WHERE id=?", (eid,)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["failed_at"] is not None  # stamped on terminal transition
