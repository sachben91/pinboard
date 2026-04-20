"""Pin/unpin operations with max-5 invariant and slot re-numbering."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from .events import record, now_utc

MAX_PINS = 5


def _new_id() -> str:
    return str(uuid.uuid4())


def active_pins(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pins WHERE unpinned_at IS NULL ORDER BY slot_order"
    ).fetchall()


def active_pin_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pins WHERE unpinned_at IS NULL"
    ).fetchone()[0]


def resolve_pin_id(conn: sqlite3.Connection, id_or_slot: str) -> str | None:
    """Accept a pin id, stream id, or 1-based slot number. Return pin id."""
    # Try as slot
    try:
        slot = int(id_or_slot)
        row = conn.execute(
            "SELECT id FROM pins WHERE unpinned_at IS NULL AND slot_order = ?", (slot,)
        ).fetchone()
        return row["id"] if row else None
    except ValueError:
        pass
    # Try as pin id
    row = conn.execute("SELECT id FROM pins WHERE id = ?", (id_or_slot,)).fetchone()
    if row:
        return row["id"]
    # Try as stream id
    row = conn.execute(
        "SELECT id FROM pins WHERE stream_id = ? AND unpinned_at IS NULL", (id_or_slot,)
    ).fetchone()
    return row["id"] if row else None


def pin_stream(conn: sqlite3.Connection, stream_id: str, note: str | None = None) -> str:
    """Pin a stream. Raises ValueError if max pins reached or already pinned."""
    count = active_pin_count(conn)
    if count >= MAX_PINS:
        raise ValueError(f"Already at maximum {MAX_PINS} active pins. Unpin one first.")

    existing = conn.execute(
        "SELECT id FROM pins WHERE stream_id = ? AND unpinned_at IS NULL", (stream_id,)
    ).fetchone()
    if existing:
        raise ValueError(f"Stream {stream_id} is already pinned.")

    stream = conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if not stream:
        raise ValueError(f"Stream {stream_id} not found.")

    slot = count + 1
    pin_id = _new_id()
    conn.execute(
        "INSERT INTO pins (id, stream_id, pinned_at, slot_order, note) VALUES (?, ?, ?, ?, ?)",
        (pin_id, stream_id, now_utc(), slot, note),
    )
    record(conn, "pin", stream_id=stream_id, pin_id=pin_id, metadata={"slot": slot})
    return pin_id


def unpin(conn: sqlite3.Connection, id_or_slot: str) -> str:
    """Unpin by pin id, stream id, or slot. Renumbers remaining slots. Returns pin_id."""
    pin_id = resolve_pin_id(conn, id_or_slot)
    if not pin_id:
        raise ValueError(f"No active pin found for: {id_or_slot}")

    pin = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
    conn.execute(
        "UPDATE pins SET unpinned_at = ? WHERE id = ?", (now_utc(), pin_id)
    )
    record(conn, "unpin", stream_id=pin["stream_id"], pin_id=pin_id)

    # Re-number remaining active pins
    remaining = conn.execute(
        "SELECT id FROM pins WHERE unpinned_at IS NULL ORDER BY slot_order"
    ).fetchall()
    for i, row in enumerate(remaining, start=1):
        conn.execute("UPDATE pins SET slot_order = ? WHERE id = ?", (i, row["id"]))

    return pin_id
