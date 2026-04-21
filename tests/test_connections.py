"""Integration test: add → auto-suggest → confirm → graph."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from pinboard.db import get_conn, DEFAULT_CHANNEL_ID
from pinboard.streams import add_stream
from pinboard.pins import pin_stream
from pinboard.connections import auto_suggest, confirm_connection, reject_connection, manual_link
from pinboard.embeddings import serialize
from pinboard.config import Config

CID = DEFAULT_CHANNEL_ID


def _vec(values: list[float]) -> bytes:
    return serialize(np.array(values, dtype=np.float32))


def _insert_stream_with_text(db, title: str, url: str, text: str, vec_values: list[float] | None = None) -> str:
    sid = add_stream(db, url, channel_id=CID, title=title)
    emb = _vec(vec_values) if vec_values else None
    db.execute("UPDATE streams SET embedding = ?, content_text = ? WHERE id = ?", (emb, text, sid))
    return sid


def test_auto_suggest_claude_connected(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory")
        pin_stream(db, CID, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about arousal")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Both explore inner states.")):
            created = auto_suggest(db, new_sid, cfg, CID)

    assert len(created) == 1


def test_auto_suggest_claude_not_connected(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory")
        pin_stream(db, CID, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about cooking")

        with patch("pinboard.connections._claude_judge", return_value=(False, "No connection.")):
            created = auto_suggest(db, new_sid, cfg, CID)

    assert len(created) == 0


def test_auto_suggest_embedding_fallback(db_path):
    cfg = Config()
    cfg.anthropic_api_key = ""
    cfg.connection_threshold = 0.5

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text", [1.0, 0.0, 0.0])
        pin_stream(db, CID, pin_sid)
        new_sid = _insert_stream_with_text(db, "Related", "https://r.com", "text", [0.9, 0.1, 0.0])
        created = auto_suggest(db, new_sid, cfg, CID)

    assert len(created) == 1


def test_confirm_connection(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory")
        pin_stream(db, CID, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about history")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_sid, cfg, CID)

        assert not db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()["confirmed"]
        confirm_connection(db, conn_id)
        assert db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()["confirmed"]


def test_reject_connection_deletes(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text")
        pin_stream(db, CID, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_sid, cfg, CID)

        reject_connection(db, conn_id)
        assert db.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone() is None


def test_manual_link(db_path):
    with get_conn(db_path) as db:
        pin_sid = add_stream(db, "https://p.com", channel_id=CID, title="Pin")
        pin_id = pin_stream(db, CID, pin_sid)
        stream_sid = add_stream(db, "https://s.com", channel_id=CID, title="Stream")

        cid = manual_link(db, stream_sid, pin_id, note="Because reasons")
        row = db.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()
        assert row["confirmed"]
        assert row["source"] == "manual"
        assert row["llm_note"] == "Because reasons"
