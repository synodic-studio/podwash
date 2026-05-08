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
