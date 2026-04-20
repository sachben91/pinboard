"""Shared fixtures for pinboard tests."""

import sqlite3
from pathlib import Path

import pytest

from pinboard.db import init_db, get_conn
from pinboard.config import Config


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "pinboard.db"
    init_db(path)
    return path


@pytest.fixture
def db(db_path):
    """Open a connection with autocommit-style context."""
    with get_conn(db_path) as conn:
        yield conn


@pytest.fixture
def cfg():
    return Config()  # default config with no API keys


@pytest.fixture
def prebuilt_db(tmp_path):
    """DB with known streams and open events for score testing."""
    from datetime import datetime, timezone, timedelta
    from pinboard.db import init_db, get_conn
    from pinboard.streams import add_stream
    from pinboard.pins import pin_stream

    path = tmp_path / "pre.db"
    init_db(path)

    with get_conn(path) as conn:
        # Add 3 streams
        s1 = add_stream(conn, "https://example.com/a", title="Article A")
        s2 = add_stream(conn, "https://example.com/b", title="Article B")
        s3 = add_stream(conn, "https://example.com/c", title="Article C")

        # Manually insert open events with controlled timestamps
        def insert_open(sid, days_ago):
            ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
                (sid, ts),
            )

        # s1: opened 3 times (recent)
        insert_open(s1, 0)
        insert_open(s1, 1)
        insert_open(s1, 2)

        # s2: opened once (2 weeks ago, at half-life boundary)
        insert_open(s2, 14)

        # s3: opened once 30 days ago (heavily decayed)
        insert_open(s3, 30)

        return path, s1, s2, s3
