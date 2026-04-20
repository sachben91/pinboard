"""Append-only event log helper. All mutations go through record()."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


EventType = str  # 'add' | 'open' | 'pin' | 'unpin' | 'edit'
                 # | 'suggest_connection' | 'confirm_connection'
                 # | 'reject_connection' | 'manual_link'


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    conn: sqlite3.Connection,
    event_type: EventType,
    *,
    stream_id: str | None = None,
    pin_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Write an event row. Call inside the same transaction as the state mutation."""
    conn.execute(
        """
        INSERT INTO events (event_type, stream_id, pin_id, metadata_json, occurred_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event_type,
            stream_id,
            pin_id,
            json.dumps(metadata) if metadata else None,
            now_utc(),
        ),
    )
