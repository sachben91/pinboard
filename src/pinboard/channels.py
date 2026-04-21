"""Channel management — create, list, switch active channel."""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from .config import PINBOARD_DIR
from .db import DEFAULT_CHANNEL_ID, DEFAULT_CHANNEL_NAME
from .events import record, now_utc

ACTIVE_CHANNEL_FILE = PINBOARD_DIR / "active_channel"


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Active channel state (stored as plain text file)
# ---------------------------------------------------------------------------

def get_active_channel_id() -> str:
    if ACTIVE_CHANNEL_FILE.exists():
        return ACTIVE_CHANNEL_FILE.read_text().strip()
    return DEFAULT_CHANNEL_ID


def set_active_channel_id(channel_id: str) -> None:
    PINBOARD_DIR.mkdir(parents=True, exist_ok=True)
    ACTIVE_CHANNEL_FILE.write_text(channel_id)


# ---------------------------------------------------------------------------
# DB operations
# ---------------------------------------------------------------------------

def create_channel(conn: sqlite3.Connection, name: str) -> str:
    existing = conn.execute("SELECT id FROM channels WHERE name = ?", (name,)).fetchone()
    if existing:
        raise ValueError(f"Channel '{name}' already exists.")
    channel_id = _new_id()
    conn.execute(
        "INSERT INTO channels (id, name, created_at) VALUES (?, ?, ?)",
        (channel_id, name, now_utc()),
    )
    record(conn, "channel_create", metadata={"name": name, "channel_id": channel_id})
    return channel_id


def list_channels(conn: sqlite3.Connection) -> list[dict]:
    active_id = get_active_channel_id()
    rows = conn.execute("SELECT id, name, created_at FROM channels ORDER BY created_at").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "created_at": r["created_at"],
            "active": r["id"] == active_id,
            "pin_count": conn.execute(
                "SELECT COUNT(*) FROM pins WHERE channel_id = ? AND unpinned_at IS NULL", (r["id"],)
            ).fetchone()[0],
            "stream_count": conn.execute(
                "SELECT COUNT(*) FROM streams WHERE channel_id = ?", (r["id"],)
            ).fetchone()[0],
        }
        for r in rows
    ]


def resolve_channel_id(conn: sqlite3.Connection, name_or_id: str) -> str:
    """Resolve a channel name or id to a channel id."""
    row = conn.execute(
        "SELECT id FROM channels WHERE name = ? OR id = ?", (name_or_id, name_or_id)
    ).fetchone()
    if not row:
        raise ValueError(f"Channel '{name_or_id}' not found.")
    return row["id"]
