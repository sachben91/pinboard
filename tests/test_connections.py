"""Integration test: add → auto-suggest → confirm → graph."""

import struct
import numpy as np
import pytest

from pinboard.db import get_conn
from pinboard.streams import add_stream
from pinboard.pins import pin_stream
from pinboard.connections import auto_suggest, confirm_connection, reject_connection, manual_link
from pinboard.embeddings import serialize
from pinboard.config import Config


def _vec(values: list[float]) -> bytes:
    arr = np.array(values, dtype=np.float32)
    return serialize(arr)


def _insert_stream_with_embedding(db, title: str, url: str, vec_values: list[float]) -> str:
    sid = add_stream(db, url, title=title)
    db.execute("UPDATE streams SET embedding = ?, content_text = ? WHERE id = ?",
               (_vec(vec_values), f"text about {title}", sid))
    return sid


def test_auto_suggest_creates_connection(db_path, cfg):
    """High-similarity stream vs pin should create a pending connection."""
    cfg.connection_threshold = 0.5
    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_embedding(db, "Pin Topic", "https://p.com", [1.0, 0.0, 0.0])
        pin_id = pin_stream(db, pin_sid)

        new_sid = _insert_stream_with_embedding(db, "Related Stream", "https://r.com", [0.9, 0.1, 0.0])
        created = auto_suggest(db, new_sid, cfg)

    assert len(created) == 1


def test_auto_suggest_below_threshold_no_connection(db_path, cfg):
    cfg.connection_threshold = 0.9
    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_embedding(db, "Pin Topic", "https://p.com", [1.0, 0.0, 0.0])
        pin_stream(db, pin_sid)

        new_sid = _insert_stream_with_embedding(db, "Unrelated Stream", "https://u.com", [0.0, 1.0, 0.0])
        created = auto_suggest(db, new_sid, cfg)

    assert len(created) == 0


def test_confirm_connection(db_path, cfg):
    cfg.connection_threshold = 0.5
    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_embedding(db, "Pin", "https://p.com", [1.0, 0.0])
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_embedding(db, "Related", "https://r.com", [0.9, 0.1])

        created = auto_suggest(db, new_sid, cfg)
        assert created
        conn_id = created[0]

        row = db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert not row["confirmed"]

        confirm_connection(db, conn_id)
        row = db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert row["confirmed"]

        # Verify event was logged
        ev = db.execute(
            "SELECT event_type FROM events WHERE event_type = 'confirm_connection'"
        ).fetchone()
        assert ev is not None


def test_reject_connection_deletes(db_path, cfg):
    cfg.connection_threshold = 0.5
    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_embedding(db, "Pin", "https://p.com", [1.0, 0.0])
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_embedding(db, "Related", "https://r.com", [0.9, 0.1])

        [conn_id] = auto_suggest(db, new_sid, cfg)
        reject_connection(db, conn_id)

        row = db.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert row is None


def test_manual_link(db_path, cfg):
    with get_conn(db_path) as db:
        pin_sid = add_stream(db, "https://p.com", title="Pin")
        pin_id = pin_stream(db, pin_sid)
        stream_sid = add_stream(db, "https://s.com", title="Stream")

        cid = manual_link(db, stream_sid, pin_id, note="Because reasons")
        row = db.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()
        assert row["confirmed"]
        assert row["source"] == "manual"
        assert row["llm_note"] == "Because reasons"
