"""Integration test: add → auto-suggest → confirm → graph."""

from __future__ import annotations

from unittest.mock import patch

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


def _insert_stream_with_text(db, title: str, url: str, text: str, vec_values: list[float] | None = None) -> str:
    sid = add_stream(db, url, title=title)
    emb = _vec(vec_values) if vec_values else None
    db.execute("UPDATE streams SET embedding = ?, content_text = ? WHERE id = ?", (emb, text, sid))
    return sid


def _cfg_with_claude(connected: bool, reason: str = "Thematically linked.") -> Config:
    """Config with mocked Claude that always returns a fixed judgment."""
    cfg = Config()
    cfg.anthropic_api_key = "fake-key"
    return cfg, connected, reason


def test_auto_suggest_claude_connected(db_path):
    """Claude says connected → connection row created."""
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory and identity")
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about arousal and history")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Both explore the body as a site of memory.")):
            created = auto_suggest(db, new_sid, cfg)

    assert len(created) == 1


def test_auto_suggest_claude_not_connected(db_path):
    """Claude says not connected → no connection row."""
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory")
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about cooking recipes")

        with patch("pinboard.connections._claude_judge", return_value=(False, "No meaningful connection.")):
            created = auto_suggest(db, new_sid, cfg)

    assert len(created) == 0


def test_auto_suggest_embedding_fallback(db_path):
    """No Anthropic key → falls back to cosine similarity."""
    cfg = Config()
    cfg.anthropic_api_key = ""
    cfg.connection_threshold = 0.5

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text", [1.0, 0.0, 0.0])
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_text(db, "Related", "https://r.com", "text", [0.9, 0.1, 0.0])
        created = auto_suggest(db, new_sid, cfg)

    assert len(created) == 1


def test_confirm_connection(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text about memory")
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text about history")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_sid, cfg)

        row = db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert not row["confirmed"]

        confirm_connection(db, conn_id)
        row = db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert row["confirmed"]

        ev = db.execute("SELECT event_type FROM events WHERE event_type = 'confirm_connection'").fetchone()
        assert ev is not None


def test_reject_connection_deletes(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_sid = _insert_stream_with_text(db, "Pin", "https://p.com", "text")
        pin_stream(db, pin_sid)
        new_sid = _insert_stream_with_text(db, "Stream", "https://s.com", "text")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_sid, cfg)

        reject_connection(db, conn_id)
        row = db.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone()
        assert row is None


def test_manual_link(db_path):
    with get_conn(db_path) as db:
        pin_sid = add_stream(db, "https://p.com", title="Pin")
        pin_id = pin_stream(db, pin_sid)
        stream_sid = add_stream(db, "https://s.com", title="Stream")

        cid = manual_link(db, stream_sid, pin_id, note="Because reasons")
        row = db.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()
        assert row["confirmed"]
        assert row["source"] == "manual"
        assert row["llm_note"] == "Because reasons"
