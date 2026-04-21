"""Shared fixtures for pinboard tests."""

import pytest

from pinboard.db import init_db, get_conn, DEFAULT_CHANNEL_ID
from pinboard.config import Config


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "pinboard.db"
    init_db(path)
    return path


@pytest.fixture
def db(db_path):
    with get_conn(db_path) as conn:
        yield conn


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def channel_id():
    return DEFAULT_CHANNEL_ID


@pytest.fixture
def prebuilt_db(tmp_path):
    """DB with known streams and open events for score testing."""
    from datetime import datetime, timezone, timedelta
    from pinboard.streams import add_stream
    from pinboard.pins import pin_stream

    path = tmp_path / "pre.db"
    init_db(path)

    with get_conn(path) as conn:
        cid = DEFAULT_CHANNEL_ID
        s1 = add_stream(conn, "https://example.com/a", channel_id=cid, title="Article A")
        s2 = add_stream(conn, "https://example.com/b", channel_id=cid, title="Article B")
        s3 = add_stream(conn, "https://example.com/c", channel_id=cid, title="Article C")

        def insert_open(sid, days_ago):
            ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
                (sid, ts),
            )

        insert_open(s1, 0)
        insert_open(s1, 1)
        insert_open(s1, 2)
        insert_open(s2, 14)
        insert_open(s3, 30)

        return path, s1, s2, s3
