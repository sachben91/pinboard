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


def link_score(conn: sqlite3.Connection, link_id: str, half_life_days: float = 14.0) -> float:
    rows = conn.execute(
        "SELECT occurred_at FROM events WHERE event_type = 'open' AND stream_id = ?",
        (link_id,),
    ).fetchall()
    return sum(math.exp(-_age_days(r["occurred_at"]) / half_life_days) for r in rows)


def pin_relevance_score(conn: sqlite3.Connection, link_id: str, stream_id: str) -> float:
    """Cosine similarity between a link and the average of all embeddings across active pin clusters."""
    from .embeddings import deserialize, cosine_similarity
    import numpy as np

    link = conn.execute("SELECT embedding FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link or not link["embedding"]:
        return 0.0

    link_vec = deserialize(link["embedding"])

    # Collect embeddings from all links in all active pin clusters
    pin_link_embeddings = conn.execute(
        """
        SELECT l.embedding FROM pin_links pl
        JOIN pins p ON p.id = pl.pin_id
        JOIN links l ON l.id = pl.link_id
        WHERE p.stream_id = ? AND p.closed_at IS NULL AND l.embedding IS NOT NULL
        """,
        (stream_id,),
    ).fetchall()

    if not pin_link_embeddings:
        return 0.0

    vecs = [deserialize(r["embedding"]) for r in pin_link_embeddings]
    avg_pin_vec = np.mean(vecs, axis=0)
    return round(cosine_similarity(link_vec, avg_pin_vec), 3)


def lab_scores(
    conn: sqlite3.Connection, stream_id: str, half_life_days: float = 14.0, limit: int = 20
) -> list[dict]:
    """Return unpinned links in the stream ranked by engagement score."""
    rows = conn.execute(
        """
        SELECT l.id, l.title, l.kind, l.source, l.created_at
        FROM links l
        WHERE l.stream_id = ?
          AND l.id NOT IN (
              SELECT pl.link_id FROM pin_links pl
              JOIN pins p ON p.id = pl.pin_id
              WHERE p.stream_id = ? AND p.closed_at IS NULL
          )
        """,
        (stream_id, stream_id),
    ).fetchall()

    results = []
    for row in rows:
        lid = row["id"]
        opens = conn.execute(
            "SELECT occurred_at FROM events WHERE event_type = 'open' AND stream_id = ?",
            (lid,),
        ).fetchall()
        score = sum(math.exp(-_age_days(r["occurred_at"]) / half_life_days) for r in opens)
        last_opened = max((r["occurred_at"] for r in opens), default=None)
        results.append({
            "id": lid,
            "title": row["title"],
            "kind": row["kind"],
            "source": row["source"],
            "score": round(score, 4),
            "open_count": len(opens),
            "last_opened": last_opened or "",
            "created_at": row["created_at"],
            "pin_score": pin_relevance_score(conn, lid, stream_id),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]
