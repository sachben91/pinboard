"""Shared fixtures for pinboard tests."""

import pytest

from pinboard.db import init_db, get_conn, DEFAULT_STREAM_ID
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
def stream_id():
    return DEFAULT_STREAM_ID


@pytest.fixture
def prebuilt_db(tmp_path):
    """DB with known links and open events for score testing."""
    from datetime import datetime, timezone, timedelta
    from pinboard.links import add_link

    path = tmp_path / "pre.db"
    init_db(path)

    with get_conn(path) as conn:
        sid = DEFAULT_STREAM_ID
        l1 = add_link(conn, "https://example.com/a", stream_id=sid, title="Article A")
        l2 = add_link(conn, "https://example.com/b", stream_id=sid, title="Article B")
        l3 = add_link(conn, "https://example.com/c", stream_id=sid, title="Article C")

        def insert_open(lid, days_ago):
            ts = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
                (lid, ts),
            )

        insert_open(l1, 0)
        insert_open(l1, 1)
        insert_open(l1, 2)
        insert_open(l2, 14)
        insert_open(l3, 30)

        return path, l1, l2, l3
