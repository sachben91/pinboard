"""Pin/unpin operations with max-3-per-channel invariant and slot re-numbering."""

from __future__ import annotations

import sqlite3
import uuid

from .events import record, now_utc

MAX_PINS_PER_CHANNEL = 3


def _new_id() -> str:
    return str(uuid.uuid4())


def active_pins(conn: sqlite3.Connection, channel_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pins WHERE channel_id = ? AND unpinned_at IS NULL ORDER BY slot_order",
        (channel_id,),
    ).fetchall()


def active_pin_count(conn: sqlite3.Connection, channel_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pins WHERE channel_id = ? AND unpinned_at IS NULL",
        (channel_id,),
    ).fetchone()[0]


def resolve_pin_id(conn: sqlite3.Connection, channel_id: str, id_or_slot: str) -> str | None:
    """Accept a pin id, stream id, or 1-based slot number. Return pin id."""
    try:
        slot = int(id_or_slot)
        row = conn.execute(
            "SELECT id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL AND slot_order = ?",
            (channel_id, slot),
        ).fetchone()
        return row["id"] if row else None
    except ValueError:
        pass
    row = conn.execute("SELECT id FROM pins WHERE id = ?", (id_or_slot,)).fetchone()
    if row:
        return row["id"]
    row = conn.execute(
        "SELECT id FROM pins WHERE stream_id = ? AND channel_id = ? AND unpinned_at IS NULL",
        (id_or_slot, channel_id),
    ).fetchone()
    return row["id"] if row else None


def pin_stream(conn: sqlite3.Connection, channel_id: str, stream_id: str, note: str | None = None) -> str:
    count = active_pin_count(conn, channel_id)
    if count >= MAX_PINS_PER_CHANNEL:
        raise ValueError(f"Channel already has {MAX_PINS_PER_CHANNEL} active pins. Unpin one first.")

    existing = conn.execute(
        "SELECT id FROM pins WHERE stream_id = ? AND channel_id = ? AND unpinned_at IS NULL",
        (stream_id, channel_id),
    ).fetchone()
    if existing:
        raise ValueError(f"Stream {stream_id} is already pinned in this channel.")

    stream = conn.execute(
        "SELECT id FROM streams WHERE id = ? AND channel_id = ?", (stream_id, channel_id)
    ).fetchone()
    if not stream:
        raise ValueError(f"Stream {stream_id} not found in this channel.")

    slot = count + 1
    pin_id = _new_id()
    conn.execute(
        "INSERT INTO pins (id, channel_id, stream_id, pinned_at, slot_order, note) VALUES (?, ?, ?, ?, ?, ?)",
        (pin_id, channel_id, stream_id, now_utc(), slot, note),
    )
    record(conn, "pin", stream_id=stream_id, pin_id=pin_id,
           metadata={"slot": slot, "channel_id": channel_id})
    return pin_id


def unpin(conn: sqlite3.Connection, channel_id: str, id_or_slot: str) -> str:
    pin_id = resolve_pin_id(conn, channel_id, id_or_slot)
    if not pin_id:
        raise ValueError(f"No active pin found for: {id_or_slot}")

    pin = conn.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
    conn.execute("UPDATE pins SET unpinned_at = ? WHERE id = ?", (now_utc(), pin_id))
    record(conn, "unpin", stream_id=pin["stream_id"], pin_id=pin_id,
           metadata={"channel_id": channel_id})

    remaining = conn.execute(
        "SELECT id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL ORDER BY slot_order",
        (channel_id,),
    ).fetchall()
    for i, row in enumerate(remaining, start=1):
        conn.execute("UPDATE pins SET slot_order = ? WHERE id = ?", (i, row["id"]))

    return pin_id
