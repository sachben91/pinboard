"""Pin cluster management — create, close, add/remove links."""

from __future__ import annotations

import sqlite3
import uuid

from .events import record, now_utc

MAX_PINS_PER_STREAM = 3
MAX_LINKS_PER_PIN = 3


def _new_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Pin cluster operations
# ---------------------------------------------------------------------------

def create_pin(conn: sqlite3.Connection, stream_id: str, name: str, note: str | None = None) -> str:
    """Create a new pin cluster. Raises ValueError if stream already has MAX_PINS_PER_STREAM active pins."""
    count = active_pin_count(conn, stream_id)
    if count >= MAX_PINS_PER_STREAM:
        raise ValueError(
            f"Stream already has {MAX_PINS_PER_STREAM} active pin clusters (the maximum). "
            "Close one before creating another."
        )
    slot = count + 1
    pin_id = _new_id()
    conn.execute(
        "INSERT INTO pins (id, stream_id, name, note, slot_order, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (pin_id, stream_id, name, note, slot, now_utc()),
    )
    record(conn, "pin", pin_id=pin_id, metadata={"name": name, "slot": slot, "stream_id": stream_id})
    return pin_id


def close_pin(conn: sqlite3.Connection, stream_id: str, id_or_slot: str) -> str:
    """Close a pin cluster. Renumbers remaining slots."""
    pin_id = resolve_pin_id(conn, stream_id, id_or_slot)
    if not pin_id:
        raise ValueError(f"No active pin cluster found for: {id_or_slot}")
    conn.execute("UPDATE pins SET closed_at = ? WHERE id = ?", (now_utc(), pin_id))
    record(conn, "unpin", pin_id=pin_id)
    _renumber_slots(conn, stream_id)
    return pin_id


def _renumber_slots(conn: sqlite3.Connection, stream_id: str) -> None:
    remaining = conn.execute(
        "SELECT id FROM pins WHERE stream_id = ? AND closed_at IS NULL ORDER BY slot_order",
        (stream_id,),
    ).fetchall()
    for i, row in enumerate(remaining, 1):
        conn.execute("UPDATE pins SET slot_order = ? WHERE id = ?", (i, row["id"]))


# ---------------------------------------------------------------------------
# Pin link operations
# ---------------------------------------------------------------------------

def add_link_to_pin(conn: sqlite3.Connection, pin_id: str, link_id: str) -> str:
    """Add a link to a pin cluster. Raises ValueError if cluster already has MAX_LINKS_PER_PIN links."""
    pin = conn.execute("SELECT id, stream_id, closed_at FROM pins WHERE id = ?", (pin_id,)).fetchone()
    if not pin:
        raise ValueError(f"Pin cluster {pin_id} not found.")
    if pin["closed_at"]:
        raise ValueError(f"Pin cluster {pin_id} is closed.")

    link = conn.execute("SELECT id, stream_id FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        raise ValueError(f"Link {link_id} not found.")
    if link["stream_id"] != pin["stream_id"]:
        raise ValueError("Link and pin cluster must be in the same stream.")

    count = conn.execute(
        "SELECT COUNT(*) FROM pin_links WHERE pin_id = ?", (pin_id,)
    ).fetchone()[0]
    if count >= MAX_LINKS_PER_PIN:
        raise ValueError(f"Pin cluster already has {MAX_LINKS_PER_PIN} links (the maximum).")

    existing = conn.execute(
        "SELECT id FROM pin_links WHERE pin_id = ? AND link_id = ?", (pin_id, link_id)
    ).fetchone()
    if existing:
        raise ValueError("Link is already in this pin cluster.")

    pl_id = _new_id()
    conn.execute(
        "INSERT INTO pin_links (id, pin_id, link_id, added_at) VALUES (?, ?, ?, ?)",
        (pl_id, pin_id, link_id, now_utc()),
    )
    record(conn, "pin", stream_id=link_id, pin_id=pin_id,
           metadata={"action": "add_link", "pin_link_id": pl_id})
    return pl_id


def remove_link_from_pin(conn: sqlite3.Connection, pin_id: str, link_id: str) -> None:
    """Remove a link from a pin cluster."""
    row = conn.execute(
        "SELECT id FROM pin_links WHERE pin_id = ? AND link_id = ?", (pin_id, link_id)
    ).fetchone()
    if not row:
        raise ValueError("Link is not in this pin cluster.")
    conn.execute("DELETE FROM pin_links WHERE pin_id = ? AND link_id = ?", (pin_id, link_id))
    record(conn, "unpin", stream_id=link_id, pin_id=pin_id, metadata={"action": "remove_link"})


# ---------------------------------------------------------------------------
# Query helpers
# ---------------------------------------------------------------------------

def active_pins(conn: sqlite3.Connection, stream_id: str) -> list[dict]:
    """Return active pin clusters with their links populated."""
    pins = conn.execute(
        "SELECT * FROM pins WHERE stream_id = ? AND closed_at IS NULL ORDER BY slot_order",
        (stream_id,),
    ).fetchall()
    result = []
    for p in pins:
        pin_links = conn.execute(
            """
            SELECT l.id, l.title, l.source, l.kind, l.artifact_path, pl.added_at
            FROM pin_links pl JOIN links l ON l.id = pl.link_id
            WHERE pl.pin_id = ?
            ORDER BY pl.added_at
            """,
            (p["id"],),
        ).fetchall()
        result.append(dict(p) | {"links": [dict(r) for r in pin_links]})
    return result


def active_pin_count(conn: sqlite3.Connection, stream_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM pins WHERE stream_id = ? AND closed_at IS NULL", (stream_id,)
    ).fetchone()[0]


def resolve_pin_id(conn: sqlite3.Connection, stream_id: str, id_or_slot: str) -> str | None:
    """Resolve a pin by slot number (1–3) or pin_id. Returns pin_id or None."""
    try:
        slot = int(id_or_slot)
        row = conn.execute(
            "SELECT id FROM pins WHERE stream_id = ? AND closed_at IS NULL AND slot_order = ?",
            (stream_id, slot),
        ).fetchone()
        if row:
            return row["id"]
    except ValueError:
        pass
    row = conn.execute(
        "SELECT id FROM pins WHERE id = ? AND stream_id = ? AND closed_at IS NULL",
        (id_or_slot, stream_id),
    ).fetchone()
    return row["id"] if row else None
