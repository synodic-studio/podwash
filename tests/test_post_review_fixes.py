"""Regression tests for post-Claude-review blockers.

Six blockers: legacy backfill, supersede-on-completion (incl. older
completed rows), scheduler SSRF, fetcher redirect validation,
atomic mark_failed_if_claimed, and concurrent upload temp paths.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import Settings
from src.database import db as db_mod
from src.database import queries
from src.database.models import Episode, Feed
from src.feeds.generator import generate_feed_xml
from src.feeds.parser import normalize_audio_url


# ---------------------------------------------------------------------------
# B1 — legacy backfill
# ---------------------------------------------------------------------------


def _legacy_db(tmp_path) -> sqlite3.Connection:
    """Build a DB that mimics the pre-migration schema: no source_identity,
    no publication_state, no is_active, no completed_at."""
    conn = sqlite3.connect(str(tmp_path / "legacy.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            source_url TEXT NOT NULL UNIQUE,
            slug TEXT NOT NULL UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
            last_polled_at TEXT,
            image_url TEXT
        );
        CREATE TABLE episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            guid TEXT NOT NULL,
            title TEXT NOT NULL,
            source_audio_url TEXT NOT NULL,
            pub_date TEXT,
            duration_seconds INTEGER,
            description TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'new',
            error_message TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0,
            original_audio_path TEXT,
            transcript_json_path TEXT,
            ad_segments_json TEXT,
            processed_audio_path TEXT,
            clean_token TEXT,
            claimed_at TEXT,
            claimed_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(feed_id, guid)
        );
        """
    )
    conn.commit()
    return conn


def test_migrate_backfills_source_identity_from_audio_url(tmp_path):
    conn = _legacy_db(tmp_path)
    conn.execute(
        "INSERT INTO feeds (name, source_url, slug) VALUES ('x', 'http://x/r', 'x')"
    )
    conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, clean_token)
        VALUES (1, 'old-guid', 't', 'https://cdn.example/show/ep1.mp3?utm_source=a',
                'completed', 'clean-tok')"""
    )
    conn.commit()
    db_mod._migrate(conn)
    row = conn.execute(
        "SELECT source_identity, publication_state FROM episodes WHERE guid='old-guid'"
    ).fetchone()
    assert row["source_identity"] == normalize_audio_url(
        "https://cdn.example/show/ep1.mp3?utm_source=a"
    )
    assert row["publication_state"] == "clean"


def test_legacy_clean_row_survives_guid_rotation_after_migrate(tmp_path):
    conn = _legacy_db(tmp_path)
    conn.execute(
        "INSERT INTO feeds (name, source_url, slug) VALUES ('x', 'http://x/r', 'x')"
    )
    conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, clean_token, pub_date)
        VALUES (1, 'g-old', 't', 'https://cdn.example/show/ep1.mp3?utm=a',
                'completed', 'clean-old', '2026-01-01T00:00:00+00:00')"""
    )
    conn.commit()
    db_mod._migrate(conn)

    # Now poll with rotated guid + tracking change.
    ep = Episode(
        feed_id=1,
        guid="g-new",
        title="t",
        source_audio_url="https://cdn.example/show/ep1.mp3?utm=b",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_identity=normalize_audio_url(
            "https://cdn.example/show/ep1.mp3?utm=b"
        ),
    )
    new_id, inserted = queries.upsert_episode_from_poll(
        conn, ep, seen_at=datetime.now(timezone.utc)
    )
    assert inserted is False
    queries.mark_feed_poll_visibility(
        conn, 1, [new_id], seen_at=datetime.now(timezone.utc)
    )

    feed = queries.get_feed_by_id(conn, 1)
    eps = queries.get_visible_episodes_for_feed(conn, 1)
    xml = generate_feed_xml(feed, eps, "http://podwash")
    assert xml.count("<item>") == 1
    assert "clean-old" in xml


# ---------------------------------------------------------------------------
# B2 — supersede earlier completed rows
# ---------------------------------------------------------------------------


@pytest.fixture
def conn(tmp_path):
    return db_mod.init_db(str(tmp_path))


def _seed_two_completed(conn, src_url):
    feed_id = queries.upsert_feed(
        conn, Feed(name="x", source_url="http://x/rss", slug="x")
    )
    si = normalize_audio_url(src_url)
    ids = []
    for guid, token in (("g1", "tok1"), ("g2", "tok2")):
        cur = conn.execute(
            """INSERT INTO episodes
            (feed_id, guid, title, source_audio_url, status, clean_token,
             source_identity, publication_state, completed_at, pub_date)
            VALUES (?, ?, 't', ?, 'completed', ?, ?, 'clean',
                    datetime('now'), '2026-01-01T00:00:00+00:00')""",
            (feed_id, guid, src_url, token, si),
        )
        ids.append(cur.lastrowid)
    conn.commit()
    return feed_id, ids


def test_duplicate_completed_rows_collapse_on_next_completion(conn):
    src = "https://cdn.example/show/ep1.mp3"
    feed_id, (older, newer) = _seed_two_completed(conn, src)

    # Completing the newer row should hide the older one — same source_identity.
    queries.hide_superseded_publications(conn, newer)
    visible = queries.get_visible_episodes_for_feed(conn, feed_id)
    assert {ep.id for ep in visible} == {newer}


def test_mark_completed_supersedes_prior_completed_row(conn):
    feed_id = queries.upsert_feed(
        conn, Feed(name="x", source_url="http://x/rss", slug="x")
    )
    src = "https://cdn.example/show/ep1.mp3"
    si = normalize_audio_url(src)
    # First completed.
    cur1 = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, clean_token,
         source_identity, publication_state, completed_at, pub_date)
        VALUES (?, 'g1', 't', ?, 'completed', 'tok1', ?, 'clean',
                datetime('now'), '2026-01-01T00:00:00+00:00')""",
        (feed_id, src, si),
    )
    older = cur1.lastrowid
    # Second row, in-flight, about to complete.
    cur2 = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status,
         source_identity, publication_state, pub_date,
         claimed_by, claim_token)
        VALUES (?, 'g2', 't', ?, 'editing', ?, 'placeholder',
                '2026-01-02T00:00:00+00:00', 'w', 'ct')""",
        (feed_id, src, si),
    )
    newer = cur2.lastrowid
    conn.commit()

    token = queries.mark_completed_if_claimed(
        conn, newer, "w", "ct", "feed_1/ep_2/processed.mp3", None
    )
    assert token is not None
    visible_ids = {ep.id for ep in queries.get_visible_episodes_for_feed(conn, feed_id)}
    assert visible_ids == {newer}
    # Older row hidden, not deleted.
    row = conn.execute(
        "SELECT publication_state, is_active FROM episodes WHERE id=?", (older,)
    ).fetchone()
    assert row["publication_state"] == "hidden"
    assert row["is_active"] == 0


# ---------------------------------------------------------------------------
# B3 — scheduler uses SSRF-safe fetcher
# ---------------------------------------------------------------------------


def test_scheduler_calls_fetch_public_feed_sync(monkeypatch, tmp_path):
    from src import scheduler

    settings = Settings()
    settings.data_dir = str(tmp_path)
    conn = db_mod.init_db(str(tmp_path))
    queries.upsert_feed(
        conn,
        Feed(name="show", source_url="https://feeds.example.com/x.rss", slug="show"),
    )

    fetched: list[str] = []

    def _fake_fetch(url, **kw):
        fetched.append(url)
        return b"<rss/>"

    monkeypatch.setattr(scheduler, "fetch_public_feed_sync", _fake_fetch)

    def _fake_parse(content, feed_id, max_episodes=0):
        return []

    monkeypatch.setattr(scheduler, "parse_feed_content", _fake_parse)
    # The legacy URL-based entry points must NOT be called from scheduler.
    monkeypatch.setattr(
        scheduler, "parse_feed", lambda *a, **kw: pytest.fail("legacy parse_feed used")
    )

    scheduler._poll_feeds(conn, settings)
    assert fetched == ["https://feeds.example.com/x.rss"]


# ---------------------------------------------------------------------------
# B4 — manual redirect validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetcher_disables_automatic_redirects(monkeypatch):
    """The httpx call must not pass follow_redirects=True."""
    from src.feeds import fetcher

    captured: dict = {}

    class _FakeResp:
        status_code = 200
        url = type("U", (), {"host": "feeds.example.com"})()
        is_redirect = False

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            for chunk in (b"<rss/>",):
                yield chunk

    class _StreamCM:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            pass

    class _FakeClient:
        def __init__(self, *a, **kw):
            captured.update(kw)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def stream(self, method, url, **kw):
            return _StreamCM(_FakeResp())

    monkeypatch.setattr(fetcher.httpx, "AsyncClient", _FakeClient)
    # Bypass DNS check.
    monkeypatch.setattr(fetcher, "_validate_hostname", lambda h: None)
    await fetcher.fetch_public_feed("https://feeds.example.com/x.rss")
    assert captured.get("follow_redirects") is False


@pytest.mark.asyncio
async def test_fetcher_rejects_redirect_to_private_ip(monkeypatch):
    import httpx as _httpx

    from src.feeds import fetcher

    class _Resp:
        status_code = 302
        headers = {"Location": "http://192.168.1.1/internal"}
        url = _httpx.URL("https://feeds.example.com/x.rss")
        is_redirect = True

        def raise_for_status(self):
            pass

        async def aiter_bytes(self):
            yield b""

    class _StreamCM:
        def __init__(self, resp):
            self._resp = resp

        async def __aenter__(self):
            return self._resp

        async def __aexit__(self, *a):
            pass

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        def stream(self, method, url, **kw):
            return _StreamCM(_Resp())

    monkeypatch.setattr(fetcher.httpx, "AsyncClient", _Client)
    # Reject the private host on validation, allow the public initial one.
    real_validate = fetcher._validate_hostname

    def _v(host):
        if host == "feeds.example.com":
            return None
        return real_validate(host)

    monkeypatch.setattr(fetcher, "_validate_hostname", _v)
    with pytest.raises(fetcher.FeedFetchError):
        await fetcher.fetch_public_feed("https://feeds.example.com/x.rss")


# ---------------------------------------------------------------------------
# B5 — mark_failed_if_claimed atomic
# ---------------------------------------------------------------------------


def test_mark_failed_if_claimed_returns_none_when_claim_cleared_between_calls(conn):
    feed_id = queries.upsert_feed(
        conn, Feed(name="x", source_url="http://x", slug="x")
    )
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status, claimed_by, claim_token)
        VALUES (?, 'g', 't', 'http://a', 'editing', 'wa', 'tA')""",
        (feed_id,),
    )
    ep_id = cur.lastrowid
    conn.commit()
    # Simulate another worker stealing the claim.
    conn.execute(
        "UPDATE episodes SET claim_token='tB', claimed_by='wb' WHERE id=?", (ep_id,)
    )
    conn.commit()
    result = queries.mark_failed_if_claimed(conn, ep_id, "wa", "tA", "boom")
    assert result is None


# ---------------------------------------------------------------------------
# B6 — concurrent uploads use distinct temp paths
# ---------------------------------------------------------------------------


def test_concurrent_uploads_with_same_claim_do_not_share_temp_path(tmp_path):
    s = Settings()
    s.data_dir = str(tmp_path)
    s.worker.token = "tok"
    s.worker.max_upload_mb = 50
    app = create_app(s)
    conn = app.state.db
    feed_id = queries.upsert_feed(
        conn, Feed(name="x", source_url="http://x", slug="x")
    )
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, status)
        VALUES (?, 'g', 't', 'http://a', 'pending')""",
        (feed_id,),
    )
    ep_id = cur.lastrowid
    conn.commit()

    with TestClient(app) as client:
        claim = client.get(
            "/api/jobs/next",
            headers={"Authorization": "Bearer tok", "X-Worker-Id": "w"},
        ).json()
        token = claim["claim_token"]

        def _upload(body):
            return client.post(
                f"/api/jobs/{ep_id}/result",
                files={"audio": ("p.mp3", body, "audio/mpeg")},
                data={"ad_segments_json": ""},
                headers={
                    "Authorization": "Bearer tok",
                    "X-Worker-Id": "w",
                    "X-Claim-Token": token,
                },
            )

        results: list[int] = []

        def _worker(payload):
            r = _upload(payload)
            results.append(r.status_code)

        t1 = threading.Thread(target=_worker, args=(b"first" * 1024,))
        t2 = threading.Thread(target=_worker, args=(b"second" * 1024,))
        t1.start()
        time.sleep(0.01)
        t2.start()
        t1.join()
        t2.join()

    # Exactly one upload wins; the other returns 409.
    assert sorted(results) == [200, 409], results
    # No leftover temp files in episode directory.
    ep_dir = tmp_path / f"feed_{feed_id}" / f"ep_{ep_id}"
    leftover = [p for p in ep_dir.iterdir() if "tmp" in p.name]
    assert leftover == []
