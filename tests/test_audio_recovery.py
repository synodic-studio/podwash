"""Test the audio route's self-heal for completed-but-missing files.

If the DB says an episode is completed but the on-disk file is gone
(manual delete, cleanup off-by-one, disk corruption), the route must:
  - flip the row back to NEW so the next tap reprocesses it
  - emit one alert (so we hear about it the first time)
  - serve the placeholder clip so the podcast app doesn't 500
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Settings
from src.database import queries


def _settings(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = "ignored"
    return s


@pytest.fixture
def app(tmp_path, monkeypatch):
    # No background scheduler in tests — the lifespan handler still runs
    # but the scheduler we don't care about; let it tick.
    settings = _settings(tmp_path)
    a = create_app(settings)
    return a


def _make_feed_and_episode(conn, *, status, processed_audio_path):
    feed_id = queries.upsert_feed(
        conn,
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
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, processed_audio_path)
        VALUES (?, 'g1', 'T', 'http://a', ?, ?)""",
        (feed_id, status, processed_audio_path),
    )
    conn.commit()
    return feed_id, cur.lastrowid


def test_completed_with_existing_file_serves_audio(app, tmp_path):
    rel_path = "feed_1/ep_1/processed.mp3"
    abs_path = tmp_path / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"id3-fake-mp3-bytes")

    feed_id, ep_id = _make_feed_and_episode(
        app.state.db, status="completed", processed_audio_path=rel_path
    )

    with TestClient(app) as client:
        r = client.get(f"/audio/{feed_id}/{ep_id}.mp3")
    assert r.status_code == 200
    assert r.content == b"id3-fake-mp3-bytes"


def test_completed_with_missing_file_resets_to_new_and_alerts(
    app, tmp_path, monkeypatch
):
    feed_id, ep_id = _make_feed_and_episode(
        app.state.db,
        status="completed",
        processed_audio_path="feed_1/ep_1/processed.mp3",
    )
    # Note: we deliberately did NOT create the file on disk.

    sent: list[dict] = []
    from src.api.routes import audio as audio_route

    monkeypatch.setattr(audio_route, "send_alert", lambda **kw: sent.append(kw) or True)

    with TestClient(app) as client:
        r = client.get(f"/audio/{feed_id}/{ep_id}.mp3")
    assert r.status_code == 200  # placeholder served, not 500

    # DB row was reset to NEW (so next tap → PENDING → worker picks up).
    row = app.state.db.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] == "new"

    # Exactly one alert with the missing path in context.
    assert len(sent) == 1
    assert sent[0]["subsystem"] == "server-audio"
    assert sent[0]["kind"] == "completed audio missing"
    assert "missing_path" in sent[0]["context"]


def test_new_episode_request_flips_to_pending(app, tmp_path):
    feed_id, ep_id = _make_feed_and_episode(
        app.state.db, status="new", processed_audio_path=None
    )
    with TestClient(app) as client:
        r = client.get(f"/audio/{feed_id}/{ep_id}.mp3")
    assert r.status_code == 200  # placeholder
    row = app.state.db.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] == "pending"
