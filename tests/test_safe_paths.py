"""Safe path containment for audio serving and cleanup."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import scheduler
from src.api.app import create_app
from src.api.routes import audio as audio_route
from src.config import Settings
from src.database import queries
from src.database.models import Feed
from src.safe_paths import UnsafeRelativePath, resolve_under_data_dir


def test_resolve_accepts_normal_relative(tmp_path):
    p = resolve_under_data_dir(tmp_path, "feed_1/ep_1/processed.mp3")
    assert str(p).startswith(str(tmp_path.resolve()))


def test_resolve_rejects_absolute(tmp_path):
    with pytest.raises(UnsafeRelativePath):
        resolve_under_data_dir(tmp_path, "/etc/passwd")


def test_resolve_rejects_dotdot_escape(tmp_path):
    with pytest.raises(UnsafeRelativePath):
        resolve_under_data_dir(tmp_path, "../../../etc/passwd")


def test_audio_route_refuses_dotdot_processed_path(tmp_path, monkeypatch):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = "x"
    app = create_app(s)

    feed_id = queries.upsert_feed(
        app.state.db, Feed(name="x", source_url="http://x", slug="x")
    )
    cur = app.state.db.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, processed_audio_path,
         clean_token)
        VALUES (?, 'g', 't', 'http://a', 'completed', '../etc/passwd', 'tok')""",
        (feed_id,),
    )
    app.state.db.commit()
    ep_id = cur.lastrowid

    sent: list[dict] = []
    monkeypatch.setattr(audio_route, "send_alert", lambda **kw: sent.append(kw))

    with TestClient(app) as client:
        r = client.get(f"/audio/{feed_id}/{ep_id}.mp3")
    # Placeholder served, no 500.
    assert r.status_code == 200
    assert sent and sent[0]["kind"] == "unsafe processed path"


def test_audio_clean_route_refuses_dotdot_processed_path(tmp_path, monkeypatch):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = "x"
    app = create_app(s)

    feed_id = queries.upsert_feed(
        app.state.db, Feed(name="x", source_url="http://x", slug="x")
    )
    app.state.db.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, processed_audio_path,
         clean_token)
        VALUES (?, 'g', 't', 'http://a', 'completed', '../etc/passwd', 'mytoken')""",
        (feed_id,),
    )
    app.state.db.commit()

    sent: list[dict] = []
    monkeypatch.setattr(audio_route, "send_alert", lambda **kw: sent.append(kw))

    with TestClient(app) as client:
        r = client.get("/audio/clean/mytoken.mp3")
    assert r.status_code == 404
    assert sent and sent[0]["kind"] == "unsafe processed path"


def test_cleanup_does_not_unlink_outside_data_dir(tmp_path, monkeypatch):
    from src.database import db as db_mod

    conn = db_mod.init_db(str(tmp_path))
    feed_id = queries.upsert_feed(
        conn, Feed(name="x", source_url="http://x", slug="x")
    )
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, processed_audio_path,
         completed_at)
        VALUES (?, 'g', 't', 'http://a', 'completed', '../escape.mp3', ?)""",
        (feed_id, "2020-01-01T00:00:00"),
    )
    conn.commit()

    sent: list[dict] = []
    monkeypatch.setattr(scheduler, "send_alert", lambda **kw: sent.append(kw))

    s = Settings()
    s.data_dir = str(tmp_path)
    s.processing.retention_days = 1
    scheduler._cleanup_old(conn, s)
    # Row left alone, alert fired.
    row = conn.execute(
        "SELECT status FROM episodes WHERE id=?", (cur.lastrowid,)
    ).fetchone()
    assert row["status"] == "completed"
    assert sent and sent[0]["kind"] == "unsafe processed path"
