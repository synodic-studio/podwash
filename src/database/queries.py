"""CRUD operations for feeds, episodes, and processing logs."""

import sqlite3
from datetime import datetime, timedelta

from .models import Episode, EpisodeStatus, Feed, ProcessingLog


def upsert_feed(conn: sqlite3.Connection, feed: Feed) -> int:
    """Insert or update a feed. Returns the feed id."""
    cursor = conn.execute(
        """INSERT INTO feeds (name, source_url, slug, enabled, poll_interval_minutes)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source_url) DO UPDATE SET
            name=excluded.name,
            slug=excluded.slug,
            enabled=excluded.enabled,
            poll_interval_minutes=excluded.poll_interval_minutes
        RETURNING id""",
        (
            feed.name,
            feed.source_url,
            feed.slug,
            int(feed.enabled),
            feed.poll_interval_minutes,
        ),
    )
    row = cursor.fetchone()
    conn.commit()
    return row[0]


def get_feed_by_slug(conn: sqlite3.Connection, slug: str) -> Feed | None:
    """Get a feed by its URL-safe slug."""
    row = conn.execute("SELECT * FROM feeds WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        return None
    return Feed(**dict(row))


def get_feed_by_id(conn: sqlite3.Connection, feed_id: int) -> Feed | None:
    """Get a feed by its ID."""
    row = conn.execute("SELECT * FROM feeds WHERE id = ?", (feed_id,)).fetchone()
    if row is None:
        return None
    return Feed(**dict(row))


def get_all_feeds(conn: sqlite3.Connection, enabled_only: bool = True) -> list[Feed]:
    """Get all feeds, optionally filtering to enabled only."""
    query = "SELECT * FROM feeds"
    if enabled_only:
        query += " WHERE enabled = 1"
    rows = conn.execute(query).fetchall()
    return [Feed(**dict(row)) for row in rows]


def update_feed_image(conn: sqlite3.Connection, feed_id: int, image_url: str) -> None:
    """Update the artwork image URL for a feed."""
    conn.execute("UPDATE feeds SET image_url = ? WHERE id = ?", (image_url, feed_id))
    conn.commit()


def delete_feed(conn: sqlite3.Connection, feed_id: int) -> bool:
    """Delete a feed and all its episodes and logs. Returns True if feed existed."""
    # Delete processing logs for all episodes in the feed
    conn.execute(
        "DELETE FROM processing_log WHERE episode_id IN (SELECT id FROM episodes WHERE feed_id = ?)",
        (feed_id,),
    )
    # Delete all episodes
    conn.execute("DELETE FROM episodes WHERE feed_id = ?", (feed_id,))
    # Delete the feed
    cursor = conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()
    return cursor.rowcount > 0


def update_feed_polled(conn: sqlite3.Connection, feed_id: int) -> None:
    """Update last_polled_at timestamp for a feed."""
    conn.execute(
        "UPDATE feeds SET last_polled_at = ? WHERE id = ?",
        (datetime.now().isoformat(), feed_id),
    )
    conn.commit()


def upsert_episode(conn: sqlite3.Connection, episode: Episode) -> int | None:
    """Insert an episode if it doesn't exist (by feed_id + guid). Returns id or None if exists.

    New rows start as 'new' — the worker only picks up rows the user has
    tapped (status='pending'). This keeps the pipeline on-demand instead of
    draining the entire backlog automatically.
    """
    cursor = conn.execute(
        """INSERT OR IGNORE INTO episodes
        (feed_id, guid, title, source_audio_url, pub_date, duration_seconds, description, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'new')""",
        (
            episode.feed_id,
            episode.guid,
            episode.title,
            episode.source_audio_url,
            episode.pub_date.isoformat() if episode.pub_date else None,
            episode.duration_seconds,
            episode.description,
        ),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None  # Already existed
    return cursor.lastrowid


def get_episodes_for_feed(conn: sqlite3.Connection, feed_id: int) -> list[Episode]:
    """Get all episodes for a feed, ordered by pub_date descending."""
    rows = conn.execute(
        "SELECT * FROM episodes WHERE feed_id = ? ORDER BY pub_date DESC",
        (feed_id,),
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def get_episode_by_id(conn: sqlite3.Connection, episode_id: int) -> Episode | None:
    """Get a single episode by ID."""
    row = conn.execute("SELECT * FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    if row is None:
        return None
    return _row_to_episode(row)


def reset_orphaned_in_flight(conn: sqlite3.Connection) -> int:
    """Reset episodes stuck in in-flight states back to pending.

    Called at startup — if the process died mid-pipeline (OOM, restart), any
    episode still marked downloading/transcribing/classifying/editing is
    orphaned and will never advance. Reset to pending so it retries.
    """
    cur = conn.execute(
        """UPDATE episodes SET status = 'pending'
        WHERE status IN ('downloading','transcribing','classifying','editing')""",
    )
    conn.commit()
    return cur.rowcount


def get_pending_episodes(conn: sqlite3.Connection, limit: int = 10) -> list[Episode]:
    """Get episodes that need processing."""
    rows = conn.execute(
        """SELECT * FROM episodes
        WHERE status = 'pending'
        ORDER BY created_at ASC
        LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def get_failed_episodes(
    conn: sqlite3.Connection, max_retries: int = 3
) -> list[Episode]:
    """Get failed episodes eligible for retry."""
    rows = conn.execute(
        """SELECT * FROM episodes
        WHERE status = 'failed' AND retry_count < ?
        ORDER BY created_at ASC""",
        (max_retries,),
    ).fetchall()
    return [_row_to_episode(row) for row in rows]


def update_episode_status(
    conn: sqlite3.Connection,
    episode_id: int,
    status: EpisodeStatus,
    error_message: str | None = None,
) -> None:
    """Update the processing status of an episode."""
    if error_message is not None:
        conn.execute(
            "UPDATE episodes SET status = ?, error_message = ?, retry_count = retry_count + 1 WHERE id = ?",
            (status.value, error_message, episode_id),
        )
    else:
        conn.execute(
            "UPDATE episodes SET status = ? WHERE id = ?",
            (status.value, episode_id),
        )
    conn.commit()


def update_episode_paths(
    conn: sqlite3.Connection,
    episode_id: int,
    *,
    original_audio_path: str | None = None,
    transcript_json_path: str | None = None,
    ad_segments_json: str | None = None,
    processed_audio_path: str | None = None,
) -> None:
    """Update file paths on an episode after processing stages."""
    updates = []
    params = []
    if original_audio_path is not None:
        updates.append("original_audio_path = ?")
        params.append(original_audio_path)
    if transcript_json_path is not None:
        updates.append("transcript_json_path = ?")
        params.append(transcript_json_path)
    if ad_segments_json is not None:
        updates.append("ad_segments_json = ?")
        params.append(ad_segments_json)
    if processed_audio_path is not None:
        updates.append("processed_audio_path = ?")
        params.append(processed_audio_path)
    if not updates:
        return
    params.append(episode_id)
    conn.execute(f"UPDATE episodes SET {', '.join(updates)} WHERE id = ?", params)
    conn.commit()


def get_processing_logs(
    conn: sqlite3.Connection, episode_id: int
) -> list[ProcessingLog]:
    """Get all processing logs for an episode, ordered chronologically."""
    rows = conn.execute(
        "SELECT * FROM processing_log WHERE episode_id = ? ORDER BY created_at ASC",
        (episode_id,),
    ).fetchall()
    return [ProcessingLog(**dict(row)) for row in rows]


def get_feed_episode_stats(conn: sqlite3.Connection) -> dict[int, dict]:
    """Get episode count stats per feed in a single query."""
    rows = conn.execute(
        """SELECT feed_id,
            COUNT(*) as total,
            SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
            SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed,
            SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed
        FROM episodes GROUP BY feed_id"""
    ).fetchall()
    return {row["feed_id"]: dict(row) for row in rows}


def add_processing_log(conn: sqlite3.Connection, log: ProcessingLog) -> int:
    """Insert a processing log entry."""
    cursor = conn.execute(
        """INSERT INTO processing_log (episode_id, stage, status, message, duration_ms)
        VALUES (?, ?, ?, ?, ?)""",
        (log.episode_id, log.stage, log.status, log.message, log.duration_ms),
    )
    conn.commit()
    return cursor.lastrowid


def queue_health_snapshot(conn: sqlite3.Connection) -> dict:
    """Return a small snapshot of queue state for the watchdog.

    Keys:
      pending_count       — rows currently waiting for a worker
      in_flight_count     — rows a worker has claimed (downloading/...)
      last_claim_at       — most recent claim across the table (str or None)
      last_claim_by       — worker id on that most-recent claim
      oldest_pending_id   — id of the longest-waiting pending row
    """
    pending_row = conn.execute(
        "SELECT COUNT(*) AS n FROM episodes WHERE status = 'pending'"
    ).fetchone()
    in_flight_row = conn.execute(
        """SELECT COUNT(*) AS n FROM episodes
        WHERE status IN ('downloading','transcribing','classifying','editing')"""
    ).fetchone()
    last_row = conn.execute(
        """SELECT claimed_at, claimed_by FROM episodes
        WHERE claimed_at IS NOT NULL
        ORDER BY claimed_at DESC LIMIT 1"""
    ).fetchone()
    oldest_row = conn.execute(
        """SELECT id FROM episodes WHERE status = 'pending'
        ORDER BY id ASC LIMIT 1"""
    ).fetchone()
    return {
        "pending_count": int(pending_row["n"]) if pending_row else 0,
        "in_flight_count": int(in_flight_row["n"]) if in_flight_row else 0,
        "last_claim_at": last_row["claimed_at"] if last_row else None,
        "last_claim_by": last_row["claimed_by"] if last_row else None,
        "oldest_pending_id": oldest_row["id"] if oldest_row else None,
    }


def reset_stale_claims(conn: sqlite3.Connection, stale_minutes: int = 45) -> int:
    """Revert episodes whose claim is older than `stale_minutes` back to pending.

    Workers claim by setting an in-flight status + claimed_at. If the worker
    dies mid-job, the claim never releases. This sweep runs before each claim
    so abandoned work gets retried.
    """
    cutoff = (datetime.now() - timedelta(minutes=stale_minutes)).isoformat()
    cur = conn.execute(
        """UPDATE episodes
        SET status = 'pending', claimed_at = NULL, claimed_by = NULL
        WHERE claimed_at IS NOT NULL
          AND claimed_at < ?
          AND status IN ('downloading','transcribing','classifying','editing')""",
        (cutoff,),
    )
    conn.commit()
    return cur.rowcount


def claim_next_pending(
    conn: sqlite3.Connection,
    worker_id: str,
    stale_minutes: int = 45,
) -> Episode | None:
    """Atomically claim the oldest pending episode for a worker.

    Sweeps stale claims first, then picks the oldest pending row and marks
    it with status='downloading' + claimed_at=now + claimed_by=worker_id.
    Returns the claimed Episode, or None if the queue is empty.
    """
    reset_stale_claims(conn, stale_minutes)
    now = datetime.now().isoformat()
    # UPDATE ... RETURNING is SQLite 3.35+; fine on any modern host.
    row = conn.execute(
        """UPDATE episodes
        SET status = 'downloading', claimed_at = ?, claimed_by = ?
        WHERE id = (
            SELECT id FROM episodes
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 1
        )
        RETURNING *""",
        (now, worker_id),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return _row_to_episode(row)


def mark_completed(
    conn: sqlite3.Connection,
    episode_id: int,
    processed_audio_path: str,
    ad_segments_json: str | None,
) -> None:
    """Finalize an episode: mark completed, save artifacts, release claim."""
    conn.execute(
        """UPDATE episodes
        SET status = 'completed',
            processed_audio_path = ?,
            ad_segments_json = COALESCE(?, ad_segments_json),
            original_audio_path = NULL,
            transcript_json_path = NULL,
            claimed_at = NULL,
            claimed_by = NULL,
            error_message = NULL
        WHERE id = ?""",
        (processed_audio_path, ad_segments_json, episode_id),
    )
    conn.commit()


def mark_failed(
    conn: sqlite3.Connection,
    episode_id: int,
    error_message: str,
    max_retries: int = 3,
) -> EpisodeStatus:
    """Record a worker failure. Bumps retry count; returns new status.

    If retry_count < max_retries, resets to pending for another attempt.
    Otherwise marks failed permanently.
    """
    row = conn.execute(
        "SELECT retry_count FROM episodes WHERE id = ?", (episode_id,)
    ).fetchone()
    if row is None:
        return EpisodeStatus.FAILED
    new_retry = (row["retry_count"] or 0) + 1
    new_status = (
        EpisodeStatus.PENDING if new_retry < max_retries else EpisodeStatus.FAILED
    )
    failed_at = (
        datetime.now().isoformat() if new_status == EpisodeStatus.FAILED else None
    )
    conn.execute(
        """UPDATE episodes
        SET status = ?, retry_count = ?, error_message = ?,
            claimed_at = NULL, claimed_by = NULL,
            failed_at = COALESCE(?, failed_at)
        WHERE id = ?""",
        (new_status.value, new_retry, error_message[:500], failed_at, episode_id),
    )
    conn.commit()
    return new_status


def count_recent_failures(conn: sqlite3.Connection, hours: int = 24) -> int:
    """Count episodes that hit terminal 'failed' status within `hours`.

    Used by the watchdog to detect a quietly-broken pipeline (bad prompt,
    upstream model regression, ffmpeg edge case) before the failures
    accumulate for days unnoticed.
    """
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat()
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM episodes
        WHERE status = 'failed' AND failed_at IS NOT NULL AND failed_at >= ?""",
        (cutoff,),
    ).fetchone()
    return int(row["n"]) if row else 0


def _row_to_episode(row: sqlite3.Row) -> Episode:
    """Convert a database row to an Episode model."""
    d = dict(row)
    if d.get("pub_date"):
        d["pub_date"] = datetime.fromisoformat(d["pub_date"])
    if d.get("created_at"):
        d["created_at"] = datetime.fromisoformat(d["created_at"])
    if d.get("claimed_at"):
        d["claimed_at"] = datetime.fromisoformat(d["claimed_at"])
    return Episode(**d)
