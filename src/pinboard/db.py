"""SQLite database initialization and connection management."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY,
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
    stream_id TEXT NOT NULL REFERENCES streams(id),
    pinned_at TIMESTAMP NOT NULL,
    unpinned_at TIMESTAMP,
    slot_order INTEGER NOT NULL,
    note TEXT
);

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
    stream_id TEXT,
    pin_id TEXT,
    metadata_json TEXT,
    occurred_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_pins_active ON pins(unpinned_at) WHERE unpinned_at IS NULL;
"""


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DDL)
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        conn.commit()


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
