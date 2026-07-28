"""Tests for worker claim ownership: only the worker that currently
owns a claim can submit a result or failure for that claim."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Settings
from src.database import queries
from src.database.models import Feed

WORKER_TOKEN = "test-token"


@pytest.fixture
def app(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = WORKER_TOKEN
    return create_app(s)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _make_pending(conn):
    feed_id = queries.upsert_feed(
        conn,
        Feed(name="show", source_url="http://show.example/rss", slug="show"),
    )
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status)
        VALUES (?, 'g1', 'T', 'http://a', 'pending')""",
        (feed_id,),
    )
    conn.commit()
    return cur.lastrowid


def _auth(worker_id: str, token: str | None = None):
    h = {
        "Authorization": f"Bearer {WORKER_TOKEN}",
        "X-Worker-Id": worker_id,
    }
    if token is not None:
        h["X-Claim-Token"] = token
    return h


def _upload(client, episode_id, *, headers, body=b"fake-mp3"):
    return client.post(
        f"/api/jobs/{episode_id}/result",
        files={"audio": ("processed.mp3", body, "audio/mpeg")},
        data={"ad_segments_json": ""},
        headers=headers,
    )


def test_claim_next_returns_claim_token(app, client):
    ep_id = _make_pending(app.state.db)
    r = client.get("/api/jobs/next", headers=_auth("worker-a"))
    assert r.status_code == 200
    body = r.json()
    assert body["episode_id"] == ep_id
    assert "claim_token" in body
    assert body["claim_token"]
    # Token is persisted on the row.
    row = app.state.db.execute(
        "SELECT claim_token FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["claim_token"] == body["claim_token"]


def test_result_without_claim_headers_returns_409(app, client):
    ep_id = _make_pending(app.state.db)
    r = _upload(client, ep_id, headers=_auth("worker-a"))
    assert r.status_code == 409
    row = app.state.db.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] == "pending"


def test_stale_worker_result_after_reclaim_returns_409(app, client, tmp_path):
    conn = app.state.db
    ep_id = _make_pending(conn)
    # Worker A claims.
    r = client.get("/api/jobs/next", headers=_auth("worker-a"))
    token_a = r.json()["claim_token"]

    # Force stale: reset and let worker B re-claim.
    queries.reset_stale_claims(conn, stale_minutes=0)
    r2 = client.get("/api/jobs/next", headers=_auth("worker-b"))
    token_b = r2.json()["claim_token"]
    assert token_b != token_a

    # Worker B completes first.
    ok = _upload(
        client, ep_id, headers=_auth("worker-b", token_b), body=b"new-result"
    )
    assert ok.status_code == 200

    # Worker A tries with stale token.
    stale = _upload(
        client, ep_id, headers=_auth("worker-a", token_a), body=b"old-result"
    )
    assert stale.status_code == 409

    # On-disk file is still the one from worker B.
    row = conn.execute(
        "SELECT processed_audio_path, clean_token FROM episodes WHERE id=?",
        (ep_id,),
    ).fetchone()
    final_path = Path(tmp_path) / row["processed_audio_path"]
    assert final_path.exists()
    assert final_path.read_bytes() == b"new-result"


def test_stale_worker_failure_after_completion_returns_409(app, client):
    conn = app.state.db
    ep_id = _make_pending(conn)
    # Worker A claims.
    r = client.get("/api/jobs/next", headers=_auth("worker-a"))
    token_a = r.json()["claim_token"]
    # Reset stale and let B claim + complete.
    queries.reset_stale_claims(conn, stale_minutes=0)
    r2 = client.get("/api/jobs/next", headers=_auth("worker-b"))
    token_b = r2.json()["claim_token"]
    ok = _upload(client, ep_id, headers=_auth("worker-b", token_b), body=b"y")
    assert ok.status_code == 200
    # Worker A submits a failure with stale token.
    r3 = client.post(
        f"/api/jobs/{ep_id}/fail",
        json={"error": "stale"},
        headers=_auth("worker-a", token_a),
    )
    assert r3.status_code == 409
    row = conn.execute("SELECT status FROM episodes WHERE id=?", (ep_id,)).fetchone()
    assert row["status"] == "completed"


def test_worker_token_uses_constant_time_compare(app, client):
    # Just exercise wrong-token path; we trust secrets.compare_digest internally.
    r = client.get(
        "/api/jobs/next",
        headers={"Authorization": "Bearer wrong-token", "X-Worker-Id": "w"},
    )
    assert r.status_code == 403


def test_mark_completed_if_claimed_returns_none_when_mismatched(app):
    conn = app.state.db
    ep_id = _make_pending(conn)
    queries.claim_next_pending(conn, "worker-a")
    out = queries.mark_completed_if_claimed(
        conn, ep_id, "worker-b", "bad-token", "x.mp3", None
    )
    assert out is None


def test_mark_failed_if_claimed_returns_none_when_mismatched(app):
    conn = app.state.db
    ep_id = _make_pending(conn)
    queries.claim_next_pending(conn, "worker-a")
    out = queries.mark_failed_if_claimed(
        conn, ep_id, "worker-b", "bad-token", "err"
    )
    assert out is None


def test_claim_round_robins_across_feeds_newest_first(tmp_path):
    """A newly added feed must not sit behind another feed's whole backlog."""
    from src.database import db as db_mod
    from src.database import queries

    conn = db_mod.init_db(tmp_path / "t.db")
    old = queries.upsert_feed(conn, Feed(name="old", source_url="http://a", slug="a"))
    new = queries.upsert_feed(conn, Feed(name="new", source_url="http://b", slug="b"))

    # 'old' has a deep backlog inserted first; 'new' is added afterwards.
    for i in range(5):
        conn.execute(
            "INSERT INTO episodes (feed_id, guid, title, source_audio_url, status,"
            " pub_date) VALUES (?,?,?,?,'pending',?)",
            (old, f"o{i}", f"old {i}", "http://x", f"2026-01-0{i + 1}T00:00:00"),
        )
    for i in range(2):
        conn.execute(
            "INSERT INTO episodes (feed_id, guid, title, source_audio_url, status,"
            " pub_date) VALUES (?,?,?,?,'pending',?)",
            (new, f"n{i}", f"new {i}", "http://x", f"2026-02-0{i + 1}T00:00:00"),
        )
    conn.commit()

    claimed = [queries.claim_next_pending(conn, "w").title for _ in range(4)]
    # Each feed's newest lands before either feed's second-newest.
    assert set(claimed[:2]) == {"old 4", "new 1"}
    assert set(claimed[2:]) == {"old 3", "new 0"}
