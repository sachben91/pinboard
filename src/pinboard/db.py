"""SQLite database initialization and connection management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT,
    artifact_path TEXT,
    content_text TEXT,
    note TEXT,
    created_at TIMESTAMP NOT NULL,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS pins (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    stream_id TEXT NOT NULL REFERENCES streams(id),
    pinned_at TIMESTAMP NOT NULL,
    unpinned_at TIMESTAMP,
    slot_order INTEGER NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS pin_skills (
    id TEXT PRIMARY KEY,
    pin_id TEXT NOT NULL REFERENCES pins(id),
    themes TEXT NOT NULL,           -- JSON array
    questions TEXT NOT NULL,        -- JSON array
    adjacent TEXT NOT NULL,         -- JSON array
    search_signals TEXT NOT NULL,   -- JSON array of targeted query strings
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pin_skills_pin ON pin_skills(pin_id);

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL REFERENCES streams(id),
    pin_id TEXT NOT NULL REFERENCES pins(id),
    similarity REAL NOT NULL,
    llm_note TEXT,
    confirmed BOOLEAN DEFAULT FALSE,
    source TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(stream_id, pin_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    channel_id TEXT,
    stream_id TEXT,
    pin_id TEXT,
    metadata_json TEXT,
    occurred_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_pins_active ON pins(unpinned_at) WHERE unpinned_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_streams_channel ON streams(channel_id);
CREATE INDEX IF NOT EXISTS idx_pins_channel ON pins(channel_id, unpinned_at);
"""

MIGRATION_V1_TO_V2 = """
-- Add channels table if missing
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL
);

-- Insert default channel
INSERT OR IGNORE INTO channels (id, name, created_at)
VALUES ('00000000-0000-0000-0000-000000000001', 'default', datetime('now'));

-- Add channel_id to streams if missing
ALTER TABLE streams ADD COLUMN channel_id TEXT REFERENCES channels(id);
UPDATE streams SET channel_id = '00000000-0000-0000-0000-000000000001' WHERE channel_id IS NULL;

-- Add channel_id to pins if missing
ALTER TABLE pins ADD COLUMN channel_id TEXT REFERENCES channels(id);
UPDATE pins SET channel_id = '00000000-0000-0000-0000-000000000001' WHERE channel_id IS NULL;

-- Add channel_id to events if missing
ALTER TABLE events ADD COLUMN channel_id TEXT;
UPDATE events SET channel_id = '00000000-0000-0000-0000-000000000001' WHERE channel_id IS NULL;
"""

MIGRATION_V2_TO_V3 = """
CREATE TABLE IF NOT EXISTS pin_skills (
    id TEXT PRIMARY KEY,
    pin_id TEXT NOT NULL REFERENCES pins(id),
    themes TEXT NOT NULL,
    questions TEXT NOT NULL,
    adjacent TEXT NOT NULL,
    search_signals TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pin_skills_pin ON pin_skills(pin_id);
"""

DEFAULT_CHANNEL_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_CHANNEL_NAME = "default"


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        # Bootstrap: ensure schema_version exists so we can check it
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.commit()

        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            # Fresh DB
            conn.executescript(DDL)
            conn.execute(
                "INSERT OR IGNORE INTO channels (id, name, created_at) VALUES (?, ?, datetime('now'))",
                (DEFAULT_CHANNEL_ID, DEFAULT_CHANNEL_NAME),
            )
            conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] < SCHEMA_VERSION:
            # Migrate first, then apply full DDL (adds missing indexes etc.)
            _migrate(conn, row[0])
            conn.executescript(DDL)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        else:
            # Up to date — just ensure any missing tables/indexes exist
            conn.executescript(DDL)
        conn.commit()


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 2:
        stmts = [s.strip() for s in MIGRATION_V1_TO_V2.split(";") if s.strip()]
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError as e:
                if "duplicate column" in str(e).lower():
                    continue
                raise
    if from_version < 3:
        stmts = [s.strip() for s in MIGRATION_V2_TO_V3.split(";") if s.strip()]
        for stmt in stmts:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass


@contextmanager
def get_conn(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
