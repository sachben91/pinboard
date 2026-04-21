"""Engagement scoring derived from the events table (never stored)."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone


def _age_days(occurred_at: str) -> float:
    try:
        dt = datetime.fromisoformat(occurred_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return max(0.0, delta.total_seconds() / 86400)
    except Exception:
        return 0.0


def stream_score(conn: sqlite3.Connection, stream_id: str, half_life_days: float = 14.0) -> float:
    rows = conn.execute(
        "SELECT occurred_at FROM events WHERE event_type = 'open' AND stream_id = ?",
        (stream_id,),
    ).fetchall()
    return sum(math.exp(-_age_days(r["occurred_at"]) / half_life_days) for r in rows)


def lab_scores(
    conn: sqlite3.Connection, channel_id: str, half_life_days: float = 14.0, limit: int = 20
) -> list[dict]:
    """Return unpinned streams in the channel ranked by engagement score."""
    rows = conn.execute(
        """
        SELECT s.id, s.title, s.kind, s.source, s.created_at
        FROM streams s
        WHERE s.channel_id = ?
          AND s.id NOT IN (
              SELECT stream_id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL
          )
        """,
        (channel_id, channel_id),
    ).fetchall()

    results = []
    for row in rows:
        sid = row["id"]
        opens = conn.execute(
            "SELECT occurred_at FROM events WHERE event_type = 'open' AND stream_id = ?",
            (sid,),
        ).fetchall()
        score = sum(math.exp(-_age_days(r["occurred_at"]) / half_life_days) for r in opens)
        last_opened = max((r["occurred_at"] for r in opens), default=None)
        results.append({
            "id": sid,
            "title": row["title"],
            "kind": row["kind"],
            "source": row["source"],
            "score": round(score, 4),
            "open_count": len(opens),
            "last_opened": last_opened or "",
            "created_at": row["created_at"],
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
