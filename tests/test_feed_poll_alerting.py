"""Tests for the per-feed consecutive-failure tracking in _poll_feeds.

The scheduler keeps a small in-memory dict of consecutive failures per
feed_id. We exercise:
  - First N-1 failures are silent (just printed).
  - The Nth consecutive failure fires exactly one alert.
  - A successful poll forgives the streak so the next failure starts
    counting from 1 again.
"""

from __future__ import annotations

import pytest

from src import scheduler
from src.config import Settings
from src.database import db, queries


@pytest.fixture
def conn(tmp_path):
    return db.init_db(str(tmp_path))


@pytest.fixture(autouse=True)
def _clear_feed_failure_counts():
    scheduler._feed_failure_counts.clear()
    yield
    scheduler._feed_failure_counts.clear()


def _make_feed(conn, slug="bad", url="http://bad.example/rss"):
    return queries.upsert_feed(
        conn,
        type(
            "F",
            (),
            {
                "name": slug,
                "source_url": url,
                "slug": slug,
                "enabled": True,
                "poll_interval_minutes": 0,
            },
        )(),
    )


def test_single_failure_does_not_alert(conn, monkeypatch):
    _make_feed(conn)

    def _boom(*a, **kw):
        raise RuntimeError("404")

    monkeypatch.setattr(scheduler, "parse_feed", _boom)
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)

    scheduler._poll_feeds(conn, Settings())
    assert sent == []
    assert scheduler._feed_failure_counts


def test_alert_fires_at_threshold(conn, monkeypatch):
    feed_id = _make_feed(conn)

    def _boom(*a, **kw):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(scheduler, "parse_feed", _boom)
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)

    for _ in range(scheduler._FEED_POLL_FAIL_THRESHOLD - 1):
        scheduler._poll_feeds(conn, Settings())
    assert sent == []  # under threshold

    scheduler._poll_feeds(conn, Settings())
    assert len(sent) == 1
    assert sent[0]["subsystem"] == "server-feed-poll"
    assert sent[0]["context"]["feed_id"] == feed_id
    assert "connection refused" in sent[0]["context"]["error"]


def test_successful_poll_resets_streak(conn, monkeypatch):
    feed_id = _make_feed(conn)
    calls: list[str] = []

    def _flaky(url, fid, max_episodes=0):
        calls.append("call")
        # Fail the first two attempts, then succeed.
        if len(calls) <= 2:
            raise RuntimeError("transient")
        return []

    monkeypatch.setattr(scheduler, "parse_feed", _flaky)
    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw) or True)

    scheduler._poll_feeds(conn, Settings())
    scheduler._poll_feeds(conn, Settings())
    scheduler._poll_feeds(conn, Settings())  # success
    assert scheduler._feed_failure_counts.get(feed_id, 0) == 0
    assert sent == []  # never reached threshold

    # Now fail again — counter should restart, not pile onto the prior 2.
    def _boom(*a, **kw):
        raise RuntimeError("again")

    monkeypatch.setattr(scheduler, "parse_feed", _boom)
    scheduler._poll_feeds(conn, Settings())
    assert scheduler._feed_failure_counts[feed_id] == 1
