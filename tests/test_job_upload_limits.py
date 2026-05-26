"""Bounded worker upload tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Settings
from src.database import queries
from src.database.models import Feed

TOKEN = "wt"


@pytest.fixture
def app(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = TOKEN
    s.worker.max_upload_mb = 1  # small for tests
    return create_app(s)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def _claim(client):
    return client.get(
        "/api/jobs/next",
        headers={"Authorization": f"Bearer {TOKEN}", "X-Worker-Id": "w"},
    ).json()


def _make_pending(conn):
    feed_id = queries.upsert_feed(
        conn, Feed(name="show", source_url="http://x", slug="x")
    )
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status)
        VALUES (?, 'g', 't', 'http://a', 'pending')""",
        (feed_id,),
    )
    conn.commit()
    return cur.lastrowid


def test_upload_over_limit_returns_413(app, client):
    ep_id = _make_pending(app.state.db)
    claim = _claim(client)
    big = b"x" * (2 * 1024 * 1024)
    r = client.post(
        f"/api/jobs/{ep_id}/result",
        files={"audio": ("p.mp3", big, "audio/mpeg")},
        data={"ad_segments_json": ""},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Worker-Id": "w",
            "X-Claim-Token": claim["claim_token"],
        },
    )
    assert r.status_code == 413
    row = app.state.db.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] != "completed"


def test_zero_byte_upload_returns_400(app, client):
    ep_id = _make_pending(app.state.db)
    claim = _claim(client)
    r = client.post(
        f"/api/jobs/{ep_id}/result",
        files={"audio": ("p.mp3", b"", "audio/mpeg")},
        data={"ad_segments_json": ""},
        headers={
            "Authorization": f"Bearer {TOKEN}",
            "X-Worker-Id": "w",
            "X-Claim-Token": claim["claim_token"],
        },
    )
    assert r.status_code == 400
    row = app.state.db.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] != "completed"
