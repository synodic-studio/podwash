"""Feed identity, dedup, and stale-pruning tests.

These cover the user-visible bug where placeholder + clean items can
both appear in the proxy RSS, and where unstable source GUIDs/URLs
create permanent duplicate rows.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.api.app import create_app
from src.config import FeedConfig, Settings
from src.database import db, queries
from src.database.models import Episode, EpisodeStatus, Feed
from src.feeds.generator import generate_feed_xml
from src.feeds.parser import build_source_identity, normalize_audio_url, parse_feed_content


@pytest.fixture
def conn(tmp_path):
    return db.init_db(str(tmp_path))


def _make_feed(conn, slug="show", url="http://show.example/rss"):
    return queries.upsert_feed(
        conn,
        Feed(name=slug, source_url=url, slug=slug),
    )


def _insert_episode(
    conn,
    feed_id,
    *,
    guid,
    title="Episode",
    audio_url="http://show.example/ep1.mp3",
    pub_date=None,
    duration=600,
    status=EpisodeStatus.NEW,
    source_identity=None,
    auto_processed=False,
):
    pd = (pub_date or datetime(2026, 1, 1, tzinfo=timezone.utc)).isoformat()
    cur = conn.execute(
        """INSERT INTO episodes
        (feed_id, guid, title, source_audio_url, pub_date, duration_seconds,
         description, status, source_identity, last_seen_at, is_active,
         publication_state, auto_processed)
        VALUES (?, ?, ?, ?, ?, ?, '', ?, ?, ?, 1, 'placeholder', ?)""",
        (
            feed_id,
            guid,
            title,
            audio_url,
            pd,
            duration,
            status.value,
            source_identity,
            datetime.now(timezone.utc).isoformat(),
            int(auto_processed),
        ),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def test_normalize_audio_url_strips_common_tracking_params():
    a = "https://cdn.example/show/ep1.mp3?utm_source=apple&utm_medium=ios"
    b = "https://cdn.example/show/ep1.mp3?utm_source=podcasts"
    assert normalize_audio_url(a) == normalize_audio_url(b)


def test_normalize_audio_url_lowercases_scheme_and_host():
    a = "HTTPS://CDN.Example.com/show/ep1.mp3"
    b = "https://cdn.example.com/show/ep1.mp3"
    assert normalize_audio_url(a) == normalize_audio_url(b)


def test_normalize_audio_url_keeps_meaningful_path():
    a = "https://cdn.example/show/ep1.mp3"
    b = "https://cdn.example/show/ep2.mp3"
    assert normalize_audio_url(a) != normalize_audio_url(b)


def test_build_source_identity_uses_normalized_audio_url_when_guid_missing():
    class FakeEntry(dict):
        def get(self, key, default=None):
            return super().get(key, default)

    entry = FakeEntry()
    url1 = "https://cdn.example/show/ep1.mp3?utm_source=a"
    url2 = "https://cdn.example/show/ep1.mp3?utm_source=b"
    assert build_source_identity(entry, url1) == build_source_identity(entry, url2)


def test_parse_feed_content_filters_entries_by_title_include_before_limit():
    rss = b"""
    <rss version="2.0"><channel><title>The Ringer-Verse</title>
      <item>
        <title>Button Mash News</title>
        <guid>button-1</guid>
        <pubDate>Wed, 03 Jun 2026 00:00:00 +0000</pubDate>
        <enclosure url="https://cdn.example/button.mp3" type="audio/mpeg" />
      </item>
      <item>
        <title>Masters of the Universe Reactions | Midnight Boys</title>
        <guid>midnight-1</guid>
        <pubDate>Tue, 02 Jun 2026 00:00:00 +0000</pubDate>
        <enclosure url="https://cdn.example/midnight-1.mp3" type="audio/mpeg" />
      </item>
      <item>
        <title>Spider-Noir Premiere Reactions | The Midnight Boys</title>
        <guid>midnight-2</guid>
        <pubDate>Mon, 01 Jun 2026 00:00:00 +0000</pubDate>
        <enclosure url="https://cdn.example/midnight-2.mp3" type="audio/mpeg" />
      </item>
    </channel></rss>
    """

    episodes = parse_feed_content(
        rss, feed_id=1, max_episodes=1, title_includes=["midnight boys"]
    )

    assert [episode.guid for episode in episodes] == ["midnight-1"]


# ---------------------------------------------------------------------------
# Generator: placeholder vs clean publication
# ---------------------------------------------------------------------------

def test_completion_replaces_placeholder_publication_with_clean_publication(conn):
    feed_id = _make_feed(conn)
    ep_id = _insert_episode(conn, feed_id, guid="source-guid")

    feed = queries.get_feed_by_id(conn, feed_id)
    episode = queries.get_episode_by_id(conn, ep_id)
    xml1 = generate_feed_xml(feed, [episode], "http://podwash")
    assert "source-guid" in xml1
    assert f"/audio/{feed_id}/{ep_id}.mp3" in xml1

    queries.mark_completed(conn, ep_id, "feed_1/ep_1/processed.mp3", None)
    episode2 = queries.get_episode_by_id(conn, ep_id)
    xml2 = generate_feed_xml(feed, [episode2], "http://podwash")
    assert f"/audio/clean/{episode2.clean_token}.mp3" in xml2
    # Keep the source GUID stable so podcast apps update one row instead of
    # showing a stale ○ placeholder next to a fresh ● clean item.
    assert "source-guid" in xml2
    assert episode2.clean_token not in xml2.replace(
        f"/audio/clean/{episode2.clean_token}.mp3", ""
    )
    assert f"/audio/{feed_id}/{ep_id}.mp3" not in xml2


def test_get_visible_episodes_filters_hidden_rows(conn):
    feed_id = _make_feed(conn)
    visible = _insert_episode(conn, feed_id, guid="g1")
    hidden = _insert_episode(conn, feed_id, guid="g2")
    conn.execute(
        "UPDATE episodes SET publication_state='hidden' WHERE id=?", (hidden,)
    )
    conn.commit()
    rows = queries.get_visible_episodes_for_feed(conn, feed_id)
    ids = {ep.id for ep in rows}
    assert visible in ids
    assert hidden not in ids


def test_get_visible_episodes_filters_inactive_rows(conn):
    feed_id = _make_feed(conn)
    active = _insert_episode(conn, feed_id, guid="g1")
    inactive = _insert_episode(conn, feed_id, guid="g2")
    conn.execute("UPDATE episodes SET is_active=0 WHERE id=?", (inactive,))
    conn.commit()
    rows = queries.get_visible_episodes_for_feed(conn, feed_id)
    ids = {ep.id for ep in rows}
    assert active in ids
    assert inactive not in ids


def test_get_visible_episodes_respects_limit(conn):
    feed_id = _make_feed(conn)
    for i in range(5):
        _insert_episode(
            conn,
            feed_id,
            guid=f"g{i}",
            pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
        )
    rows = queries.get_visible_episodes_for_feed(conn, feed_id, limit=2)
    assert len(rows) == 2


def test_auto_processed_completion_uses_pipe_title_prefix(conn):
    feed_id = _make_feed(conn)
    ep_id = _insert_episode(
        conn,
        feed_id,
        guid="auto-guid",
        title="Fresh Episode",
        auto_processed=True,
    )
    queries.mark_completed(conn, ep_id, "feed_1/ep_1/processed.mp3", None)

    feed = queries.get_feed_by_id(conn, feed_id)
    episode = queries.get_episode_by_id(conn, ep_id)
    xml = generate_feed_xml(feed, [episode], "http://podwash")

    assert "<title>|Fresh Episode</title>" in xml
    assert "● Fresh Episode" not in xml


def test_poll_inserted_episodes_are_hidden_pending_auto_processed(conn):
    feed_id = _make_feed(conn)
    ep = Episode(
        feed_id=feed_id,
        guid="new-auto",
        title="Brand New",
        source_audio_url="https://cdn.example/show/new-auto.mp3",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
        source_identity="https://cdn.example/show/new-auto.mp3",
    )

    ep_id, inserted = queries.upsert_episode_from_poll(
        conn, ep, seen_at=datetime.now(timezone.utc)
    )

    assert inserted is True
    row = queries.get_episode_by_id(conn, ep_id)
    assert row.status == EpisodeStatus.PENDING
    assert row.publication_state == "hidden"
    assert row.auto_processed is True
    assert queries.get_visible_episodes_for_feed(conn, feed_id) == []


# ---------------------------------------------------------------------------
# Polling: merge / update / hide
# ---------------------------------------------------------------------------

def test_upsert_episode_from_poll_inserts_new_row(conn):
    feed_id = _make_feed(conn)
    ep = Episode(
        feed_id=feed_id,
        guid="g1",
        title="Title",
        source_audio_url="https://cdn.example/show/ep1.mp3",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
    )
    ep.source_identity = normalize_audio_url(ep.source_audio_url)
    seen_at = datetime.now(timezone.utc)
    ep_id, inserted = queries.upsert_episode_from_poll(conn, ep, seen_at=seen_at)
    assert inserted is True
    row = queries.get_episode_by_id(conn, ep_id)
    assert row.guid == "g1"
    assert row.is_active is True


def test_upsert_episode_from_poll_merges_same_audio_with_different_tracking(conn):
    feed_id = _make_feed(conn)
    seen_at = datetime.now(timezone.utc)

    ep1 = Episode(
        feed_id=feed_id,
        guid="g1",
        title="Title v1",
        source_audio_url="https://cdn.example/show/ep1.mp3?utm=a",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
    )
    ep1.source_identity = normalize_audio_url(ep1.source_audio_url)
    id1, ins1 = queries.upsert_episode_from_poll(conn, ep1, seen_at=seen_at)
    assert ins1 is True

    ep2 = Episode(
        feed_id=feed_id,
        guid="g2-changed",
        title="Title v2",
        source_audio_url="https://cdn.example/show/ep1.mp3?utm=b",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
    )
    ep2.source_identity = normalize_audio_url(ep2.source_audio_url)
    id2, ins2 = queries.upsert_episode_from_poll(conn, ep2, seen_at=seen_at)
    assert ins2 is False
    assert id2 == id1

    # Metadata refreshed; processing state preserved.
    row = queries.get_episode_by_id(conn, id1)
    assert row.guid == "g2-changed"
    assert row.title == "Title v2"


def test_mark_feed_poll_visibility_hides_absent_rows(conn):
    feed_id = _make_feed(conn)
    keep = _insert_episode(conn, feed_id, guid="keep")
    drop = _insert_episode(conn, feed_id, guid="drop")
    queries.mark_feed_poll_visibility(
        conn, feed_id, [keep], seen_at=datetime.now(timezone.utc)
    )
    keep_row = queries.get_episode_by_id(conn, keep)
    drop_row = queries.get_episode_by_id(conn, drop)
    assert keep_row.is_active is True
    assert drop_row.is_active is False


# ---------------------------------------------------------------------------
# Cleanup semantics
# ---------------------------------------------------------------------------

def test_mark_completed_sets_completed_at(conn):
    feed_id = _make_feed(conn)
    ep_id = _insert_episode(conn, feed_id, guid="g1")
    queries.mark_completed(conn, ep_id, "feed_1/ep_1/processed.mp3", None)
    row = conn.execute(
        "SELECT completed_at FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["completed_at"] is not None


def test_cleanup_uses_completed_at_not_created_at(tmp_path, conn):
    from src import scheduler

    feed_id = _make_feed(conn)
    ep_id = _insert_episode(conn, feed_id, guid="g1")
    # Mark completed but set completed_at far in the past — and created_at recent.
    queries.mark_completed(conn, ep_id, "feed_1/ep_1/processed.mp3", None)
    far_past = (datetime.now() - timedelta(days=400)).isoformat()
    conn.execute(
        "UPDATE episodes SET completed_at=?, created_at=? WHERE id=?",
        (far_past, datetime.now().isoformat(), ep_id),
    )
    conn.commit()
    # File on disk so cleanup actually unlinks.
    abs_path = tmp_path / "feed_1/ep_1/processed.mp3"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"x")

    settings = Settings()
    settings.data_dir = str(tmp_path)
    settings.processing.retention_days = 30

    scheduler._cleanup_old(conn, settings)

    row = conn.execute(
        "SELECT status, processed_audio_path, clean_token FROM episodes WHERE id=?",
        (ep_id,),
    ).fetchone()
    assert row["status"] == "new"
    assert row["processed_audio_path"] is None
    # Stale clean_token cleared when file removed.
    assert row["clean_token"] is None


def test_cleanup_preserves_recently_completed(tmp_path, conn):
    from src import scheduler

    feed_id = _make_feed(conn)
    ep_id = _insert_episode(conn, feed_id, guid="g1")
    queries.mark_completed(conn, ep_id, "feed_1/ep_1/processed.mp3", None)
    # completed_at is now() by default; created_at could be old.
    old_created = (datetime.now() - timedelta(days=400)).isoformat()
    conn.execute(
        "UPDATE episodes SET created_at=? WHERE id=?", (old_created, ep_id)
    )
    conn.commit()
    abs_path = tmp_path / "feed_1/ep_1/processed.mp3"
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_bytes(b"x")

    settings = Settings()
    settings.data_dir = str(tmp_path)
    settings.processing.retention_days = 30

    scheduler._cleanup_old(conn, settings)

    row = conn.execute(
        "SELECT status FROM episodes WHERE id=?", (ep_id,)
    ).fetchone()
    assert row["status"] == "completed"


def test_cleanup_keeps_latest_eight_auto_processed_even_when_old(tmp_path, conn):
    from src import scheduler

    feed_id = _make_feed(conn)
    ids = []
    base_date = datetime.now(timezone.utc) - timedelta(days=70)
    for i in range(9):
        ep_id = _insert_episode(
            conn,
            feed_id,
            guid=f"auto-{i}",
            pub_date=base_date + timedelta(days=i),
            auto_processed=True,
        )
        rel_path = f"feed_1/ep_{ep_id}/processed.mp3"
        queries.mark_completed(conn, ep_id, rel_path, None)
        abs_path = tmp_path / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"x")
        ids.append(ep_id)
    conn.commit()

    settings = Settings()
    settings.data_dir = str(tmp_path)
    scheduler._cleanup_old(conn, settings)

    oldest = queries.get_episode_by_id(conn, ids[0])
    newest = [queries.get_episode_by_id(conn, ep_id) for ep_id in ids[1:]]
    assert oldest.status == EpisodeStatus.NEW
    assert oldest.auto_processed is False
    assert all(ep.status == EpisodeStatus.COMPLETED for ep in newest)
    assert all(ep.auto_processed is True for ep in newest)


# ---------------------------------------------------------------------------
# Feed route + max_episodes
# ---------------------------------------------------------------------------

def _build_app(tmp_path, *, max_episodes=0, slug="show"):
    settings = Settings()
    settings.data_dir = str(tmp_path)
    settings.feeds = [
        FeedConfig(
            name=slug, url="http://show.example/rss", slug=slug, max_episodes=max_episodes
        )
    ]
    return create_app(settings), settings


def test_feed_route_only_emits_active_rows(tmp_path):
    app, _ = _build_app(tmp_path)
    conn = app.state.db
    feed_id = queries.upsert_feed(
        conn,
        Feed(name="show", source_url="http://show.example/rss", slug="show"),
    )
    _insert_episode(conn, feed_id, guid="visible")
    hidden = _insert_episode(conn, feed_id, guid="hidden")
    conn.execute("UPDATE episodes SET is_active=0 WHERE id=?", (hidden,))
    conn.commit()
    with TestClient(app) as client:
        r = client.get("/feeds/show.xml")
    assert r.status_code == 200
    assert "visible" in r.text
    assert "hidden" not in r.text


def test_max_episodes_limits_generated_rss(tmp_path):
    app, _ = _build_app(tmp_path, max_episodes=2)
    conn = app.state.db
    feed_id = queries.upsert_feed(
        conn,
        Feed(name="show", source_url="http://show.example/rss", slug="show"),
    )
    for i in range(5):
        _insert_episode(
            conn,
            feed_id,
            guid=f"g{i}",
            pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(days=i),
        )
    with TestClient(app) as client:
        r = client.get("/feeds/show.xml")
    assert r.status_code == 200
    item_count = r.text.count("<item>")
    assert item_count == 2
    # Most recent two by pub_date are kept.
    assert "g4" in r.text and "g3" in r.text
    assert "g0" not in r.text and "g1" not in r.text


# ---------------------------------------------------------------------------
# End-to-end: poll + complete + repoll
# ---------------------------------------------------------------------------

def test_repeated_polls_and_completion_produce_single_visible_item(conn):
    feed_id = _make_feed(conn)
    seen_at = datetime.now(timezone.utc)

    ep1 = Episode(
        feed_id=feed_id,
        guid="g-original",
        title="Episode",
        source_audio_url="https://cdn.example/show/ep1.mp3?utm=a",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
    )
    ep1.source_identity = normalize_audio_url(ep1.source_audio_url)
    id1, _ = queries.upsert_episode_from_poll(conn, ep1, seen_at=seen_at)
    queries.mark_feed_poll_visibility(conn, feed_id, [id1], seen_at=seen_at)

    feed = queries.get_feed_by_id(conn, feed_id)
    eps = queries.get_visible_episodes_for_feed(conn, feed_id)
    xml1 = generate_feed_xml(feed, eps, "http://podwash")
    assert xml1.count("<item>") == 0

    # Mark completed
    queries.mark_completed(conn, id1, "feed_1/ep_1/processed.mp3", None)
    eps2 = queries.get_visible_episodes_for_feed(conn, feed_id)
    xml2 = generate_feed_xml(feed, eps2, "http://podwash")
    assert xml2.count("<item>") == 1
    assert "g-original" in xml2

    # Second poll: same audio URL different tracking + different guid.
    ep2 = Episode(
        feed_id=feed_id,
        guid="g-changed",
        title="Episode v2",
        source_audio_url="https://cdn.example/show/ep1.mp3?utm=b",
        pub_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        duration_seconds=600,
    )
    ep2.source_identity = normalize_audio_url(ep2.source_audio_url)
    id2, ins2 = queries.upsert_episode_from_poll(
        conn, ep2, seen_at=datetime.now(timezone.utc)
    )
    assert ins2 is False
    assert id2 == id1

    eps3 = queries.get_visible_episodes_for_feed(conn, feed_id)
    xml3 = generate_feed_xml(feed, eps3, "http://podwash")
    assert xml3.count("<item>") == 1
