"""Stream (workspace) management — create, list, switch active stream."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from .db import DEFAULT_STREAM_ID
from .events import record, now_utc

_ACTIVE_FILE = Path.home() / ".pinboard" / "active_stream"


def _new_id() -> str:
    return str(uuid.uuid4())


def create_stream(conn: sqlite3.Connection, name: str) -> str:
    existing = conn.execute("SELECT id FROM streams WHERE name = ?", (name,)).fetchone()
    if existing:
        raise ValueError(f"Stream '{name}' already exists.")
    stream_id = _new_id()
    conn.execute(
        "INSERT INTO streams (id, name, created_at) VALUES (?, ?, ?)",
        (stream_id, name, now_utc()),
    )
    record(conn, "channel_create", metadata={"name": name, "stream_id": stream_id})
    return stream_id


def list_streams(conn: sqlite3.Connection) -> list[dict]:
    active_id = get_active_stream_id()
    rows = conn.execute("""
        SELECT s.id, s.name, s.created_at,
               COUNT(DISTINCT p.id) FILTER (WHERE p.closed_at IS NULL) as pin_count,
               COUNT(DISTINCT l.id) as link_count
        FROM streams s
        LEFT JOIN pins p ON p.stream_id = s.id
        LEFT JOIN links l ON l.stream_id = s.id
        GROUP BY s.id
        ORDER BY s.created_at
    """).fetchall()
    return [dict(r) | {"active": r["id"] == active_id} for r in rows]


def get_active_stream_id() -> str:
    try:
        sid = _ACTIVE_FILE.read_text().strip()
        return sid if sid else DEFAULT_STREAM_ID
    except FileNotFoundError:
        return DEFAULT_STREAM_ID


def set_active_stream_id(stream_id: str) -> None:
    _ACTIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_FILE.write_text(stream_id)


def resolve_stream_id(conn: sqlite3.Connection, name_or_id: str) -> str:
    row = conn.execute(
        "SELECT id FROM streams WHERE name = ? OR id = ?", (name_or_id, name_or_id)
    ).fetchone()
    if not row:
        raise ValueError(f"Stream '{name_or_id}' not found.")
    return row["id"]
