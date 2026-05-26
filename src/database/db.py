"""SQLite database connection and schema management."""

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    source_url TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1,
    poll_interval_minutes INTEGER NOT NULL DEFAULT 60,
    last_polled_at TEXT,
    image_url TEXT
);

CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id INTEGER NOT NULL REFERENCES feeds(id),
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
    clean_token TEXT UNIQUE,
    claimed_at TEXT,
    claimed_by TEXT,
    claim_token TEXT,
    source_identity TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    publication_state TEXT NOT NULL DEFAULT 'placeholder',
    last_seen_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(feed_id, guid)
);

CREATE TABLE IF NOT EXISTS processing_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    stage TEXT NOT NULL,
    status TEXT NOT NULL,
    message TEXT DEFAULT '',
    duration_ms INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_episodes_feed_id ON episodes(feed_id);
CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_processing_log_episode_id ON processing_log(episode_id);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    """Get a SQLite connection with WAL mode and foreign keys enabled."""
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(data_dir: str) -> sqlite3.Connection:
    """Initialize the database, creating schema if needed."""
    path = Path(data_dir)
    path.mkdir(parents=True, exist_ok=True)
    _rename_legacy_db(path)
    db_path = str(path / "podwash.db")
    conn = get_connection(db_path)
    conn.executescript(SCHEMA_SQL)
    _migrate(conn)
    conn.commit()
    return conn


def _rename_legacy_db(path: Path) -> None:
    """Rename the pre-rebrand `podcast_adskip.db` to `podwash.db` in place.

    Runs before we open a connection, so WAL/SHM siblings move together.
    """
    new_db = path / "podwash.db"
    legacy_db = path / "podcast_adskip.db"
    if new_db.exists() or not legacy_db.exists():
        return
    legacy_db.rename(new_db)
    for suffix in ("-wal", "-shm"):
        sidecar = path / f"podcast_adskip.db{suffix}"
        if sidecar.exists():
            sidecar.rename(path / f"podwash.db{suffix}")


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply idempotent column-adds for existing DBs.

    SQLite has no ALTER TABLE ADD COLUMN IF NOT EXISTS, so inspect
    table_info and add missing columns. Safe to run on every boot.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(episodes)")}
    for name, ddl in (
        ("claimed_at", "ALTER TABLE episodes ADD COLUMN claimed_at TEXT"),
        ("claimed_by", "ALTER TABLE episodes ADD COLUMN claimed_by TEXT"),
        # failed_at lets the watchdog count "how many failures in last
        # 24h" — without it, a quietly-broken classifier could rot every
        # episode and we'd never know.
        ("failed_at", "ALTER TABLE episodes ADD COLUMN failed_at TEXT"),
        ("clean_token", "ALTER TABLE episodes ADD COLUMN clean_token TEXT"),
        ("claim_token", "ALTER TABLE episodes ADD COLUMN claim_token TEXT"),
        ("source_identity", "ALTER TABLE episodes ADD COLUMN source_identity TEXT"),
        (
            "is_active",
            "ALTER TABLE episodes ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1",
        ),
        (
            "publication_state",
            "ALTER TABLE episodes ADD COLUMN publication_state TEXT NOT NULL DEFAULT 'placeholder'",
        ),
        ("last_seen_at", "ALTER TABLE episodes ADD COLUMN last_seen_at TEXT"),
        ("completed_at", "ALTER TABLE episodes ADD COLUMN completed_at TEXT"),
    ):
        if name not in cols:
            conn.execute(ddl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_claimed_at ON episodes(claimed_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_failed_at ON episodes(failed_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_episodes_clean_token ON episodes(clean_token)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_feed_active_pub ON episodes(feed_id, is_active, pub_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_source_identity ON episodes(feed_id, source_identity)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_episodes_claim_token ON episodes(claim_token)"
    )
    _backfill_post_migration(conn)


def _backfill_post_migration(conn: sqlite3.Connection) -> None:
    """Populate identity fields for legacy rows the ALTER TABLE just added.

    A completed row with ``source_identity=NULL`` is invisible to the
    poll-time dedup query; the next poll with a rotated GUID inserts a
    second row and the old clean publication gets hidden. Backfill so
    legacy rows participate in dedup from the first poll after upgrade.

    Also fixes up ``publication_state`` for legacy rows: completed +
    clean_token → ``clean``; everything else stays at the default
    (``placeholder``) which is correct for not-yet-processed rows.
    """
    # Lazy import: db.py is loaded before parser.py is on most import paths,
    # and we don't want to introduce a hard cycle just for this helper.
    from src.feeds.parser import normalize_audio_url

    rows = conn.execute(
        "SELECT id, source_audio_url FROM episodes "
        "WHERE source_identity IS NULL OR source_identity = ''"
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE episodes SET source_identity = ? WHERE id = ?",
            (normalize_audio_url(row["source_audio_url"] or ""), row["id"]),
        )
    conn.execute(
        """UPDATE episodes
        SET publication_state = 'clean'
        WHERE status = 'completed'
          AND clean_token IS NOT NULL
          AND clean_token != ''
          AND publication_state != 'clean'
          AND publication_state != 'hidden'"""
    )
    # Backfill completed_at for rows that completed before we tracked it —
    # fall back to created_at so retention math doesn't churn old rows.
    conn.execute(
        """UPDATE episodes
        SET completed_at = COALESCE(completed_at, created_at)
        WHERE status = 'completed' AND completed_at IS NULL"""
    )
    # Collapse pre-existing duplicates so the first /feeds/*.xml after
    # upgrade doesn't emit two items for the same source.
    _collapse_legacy_duplicates(conn)


def _collapse_legacy_duplicates(conn: sqlite3.Connection) -> None:
    """Hide all-but-the-newest active row per (feed_id, source_identity).

    Prefer the most recent completed row (by completed_at, then id).
    Falls back to the highest id if none are completed. Existing
    ``publication_state='hidden'`` rows stay hidden.
    """
    groups = conn.execute(
        """SELECT feed_id, source_identity, COUNT(*) AS n
        FROM episodes
        WHERE source_identity IS NOT NULL AND source_identity != ''
          AND is_active = 1 AND publication_state != 'hidden'
        GROUP BY feed_id, source_identity
        HAVING n > 1"""
    ).fetchall()
    for grp in groups:
        rows = conn.execute(
            """SELECT id, status, completed_at FROM episodes
            WHERE feed_id = ? AND source_identity = ?
              AND is_active = 1 AND publication_state != 'hidden'""",
            (grp["feed_id"], grp["source_identity"]),
        ).fetchall()
        # Newest completed wins; otherwise highest id.
        completed = [r for r in rows if r["status"] == "completed"]
        if completed:
            keeper = max(completed, key=lambda r: (r["completed_at"] or "", r["id"]))
        else:
            keeper = max(rows, key=lambda r: r["id"])
        for r in rows:
            if r["id"] == keeper["id"]:
                continue
            conn.execute(
                "UPDATE episodes SET publication_state='hidden', is_active=0 WHERE id=?",
                (r["id"],),
            )
