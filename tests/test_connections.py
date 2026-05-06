"""Integration test: add → auto-suggest → confirm → graph."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from pinboard.db import get_conn, DEFAULT_STREAM_ID
from pinboard.links import add_link
from pinboard.pins import create_pin, add_link_to_pin
from pinboard.connections import auto_suggest, confirm_connection, reject_connection, manual_link
from pinboard.embeddings import serialize
from pinboard.config import Config

SID = DEFAULT_STREAM_ID


def _vec(values: list[float]) -> bytes:
    return serialize(np.array(values, dtype=np.float32))


def _insert_link_with_text(db, title: str, url: str, text: str, vec_values: list[float] | None = None) -> str:
    lid = add_link(db, url, stream_id=SID, title=title)
    emb = _vec(vec_values) if vec_values else None
    db.execute("UPDATE links SET embedding = ?, content_text = ? WHERE id = ?", (emb, text, lid))
    return lid


def _make_pin_with_link(db, link_id: str, name: str = "Test Pin") -> str:
    """Create a pin cluster and add a link to it. Returns pin_id."""
    pin_id = create_pin(db, SID, name)
    add_link_to_pin(db, pin_id, link_id)
    return pin_id


def test_auto_suggest_claude_connected(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_lid = _insert_link_with_text(db, "Pin", "https://p.com", "text about memory")
        _make_pin_with_link(db, pin_lid)
        new_lid = _insert_link_with_text(db, "Stream", "https://s.com", "text about arousal")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Both explore inner states.")):
            created = auto_suggest(db, new_lid, cfg, SID)

    assert len(created) == 1


def test_auto_suggest_claude_not_connected(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_lid = _insert_link_with_text(db, "Pin", "https://p.com", "text about memory")
        _make_pin_with_link(db, pin_lid)
        new_lid = _insert_link_with_text(db, "Stream", "https://s.com", "text about cooking")

        with patch("pinboard.connections._claude_judge", return_value=(False, "No connection.")):
            created = auto_suggest(db, new_lid, cfg, SID)

    assert len(created) == 0


def test_auto_suggest_embedding_fallback(db_path):
    cfg = Config()
    cfg.anthropic_api_key = ""
    cfg.connection_threshold = 0.5

    with get_conn(db_path) as db:
        pin_lid = _insert_link_with_text(db, "Pin", "https://p.com", "text", [1.0, 0.0, 0.0])
        _make_pin_with_link(db, pin_lid)
        new_lid = _insert_link_with_text(db, "Related", "https://r.com", "text", [0.9, 0.1, 0.0])
        created = auto_suggest(db, new_lid, cfg, SID)

    assert len(created) == 1


def test_confirm_connection(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_lid = _insert_link_with_text(db, "Pin", "https://p.com", "text about memory")
        _make_pin_with_link(db, pin_lid)
        new_lid = _insert_link_with_text(db, "Stream", "https://s.com", "text about history")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_lid, cfg, SID)

        assert not db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()["confirmed"]
        confirm_connection(db, conn_id)
        assert db.execute("SELECT confirmed FROM connections WHERE id = ?", (conn_id,)).fetchone()["confirmed"]


def test_reject_connection_deletes(db_path):
    cfg = Config()
    cfg.anthropic_api_key = "fake"

    with get_conn(db_path) as db:
        pin_lid = _insert_link_with_text(db, "Pin", "https://p.com", "text")
        _make_pin_with_link(db, pin_lid)
        new_lid = _insert_link_with_text(db, "Stream", "https://s.com", "text")

        with patch("pinboard.connections._claude_judge", return_value=(True, "Connected.")):
            [conn_id] = auto_suggest(db, new_lid, cfg, SID)

        reject_connection(db, conn_id)
        assert db.execute("SELECT id FROM connections WHERE id = ?", (conn_id,)).fetchone() is None


def test_manual_link(db_path):
    with get_conn(db_path) as db:
        pin_lid = add_link(db, "https://p.com", stream_id=SID, title="Pin")
        pin_id = _make_pin_with_link(db, pin_lid)
        new_lid = add_link(db, "https://s.com", stream_id=SID, title="Link")

        cid = manual_link(db, new_lid, pin_id, note="Because reasons")
        row = db.execute("SELECT * FROM connections WHERE id = ?", (cid,)).fetchone()
        assert row["confirmed"]
        assert row["source"] == "manual"
        assert row["llm_note"] == "Because reasons"
