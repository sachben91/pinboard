"""Semantic connection detection between streams and pins."""

from __future__ import annotations

import sqlite3
import uuid

from .embeddings import deserialize, cosine_similarity
from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


def _llm_note(cfg, stream_excerpt: str, pin_excerpt: str) -> str | None:
    if not cfg.anthropic_api_key:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=120,
            messages=[{
                "role": "user",
                "content": (
                    "In 1-2 sentences, explain the connection between these two passages.\n\n"
                    f"Passage A (stream):\n{stream_excerpt[:600]}\n\n"
                    f"Passage B (pin):\n{pin_excerpt[:600]}\n\n"
                    "Connection:"
                ),
            }],
        )
        return msg.content[0].text.strip()
    except Exception:
        return None


def auto_suggest(conn: sqlite3.Connection, stream_id: str, cfg) -> list[str]:
    """Compute similarity vs active pins; create connection rows where above threshold.
    Returns list of created connection ids."""
    stream = conn.execute(
        "SELECT embedding, content_text FROM streams WHERE id = ?", (stream_id,)
    ).fetchone()
    if not stream or not stream["embedding"]:
        return []

    stream_vec = deserialize(stream["embedding"])
    stream_excerpt = (stream["content_text"] or "")[:600]

    active_pins = conn.execute(
        """
        SELECT p.id as pin_id, s.embedding, s.content_text
        FROM pins p
        JOIN streams s ON s.id = p.stream_id
        WHERE p.unpinned_at IS NULL AND s.embedding IS NOT NULL
        """
    ).fetchall()

    created = []
    for pin_row in active_pins:
        pin_vec = deserialize(pin_row["embedding"])
        sim = cosine_similarity(stream_vec, pin_vec)
        if sim < cfg.connection_threshold:
            continue

        # Skip if connection already exists
        existing = conn.execute(
            "SELECT id FROM connections WHERE stream_id = ? AND pin_id = ?",
            (stream_id, pin_row["pin_id"]),
        ).fetchone()
        if existing:
            continue

        pin_excerpt = (pin_row["content_text"] or "")[:600]
        note = _llm_note(cfg, stream_excerpt, pin_excerpt)

        conn_id = _new_id()
        conn.execute(
            """
            INSERT INTO connections (id, stream_id, pin_id, similarity, llm_note, confirmed, source, created_at)
            VALUES (?, ?, ?, ?, ?, FALSE, 'auto', ?)
            """,
            (conn_id, stream_id, pin_row["pin_id"], sim, note, now_utc()),
        )
        record(
            conn,
            "suggest_connection",
            stream_id=stream_id,
            pin_id=pin_row["pin_id"],
            metadata={"similarity": sim, "connection_id": conn_id},
        )
        created.append(conn_id)
    return created


def confirm_connection(conn: sqlite3.Connection, conn_id: str) -> None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row:
        raise ValueError(f"Connection {conn_id} not found.")
    conn.execute("UPDATE connections SET confirmed = TRUE WHERE id = ?", (conn_id,))
    record(conn, "confirm_connection", stream_id=row["stream_id"], pin_id=row["pin_id"],
           metadata={"connection_id": conn_id})


def reject_connection(conn: sqlite3.Connection, conn_id: str) -> None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row:
        raise ValueError(f"Connection {conn_id} not found.")
    conn.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
    record(conn, "reject_connection", stream_id=row["stream_id"], pin_id=row["pin_id"],
           metadata={"connection_id": conn_id})


def manual_link(
    conn: sqlite3.Connection, stream_id: str, pin_id: str, note: str | None = None
) -> str:
    stream = conn.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if not stream:
        raise ValueError(f"Stream {stream_id} not found.")
    pin = conn.execute("SELECT id FROM pins WHERE id = ?", (pin_id,)).fetchone()
    if not pin:
        raise ValueError(f"Pin {pin_id} not found.")

    existing = conn.execute(
        "SELECT id FROM connections WHERE stream_id = ? AND pin_id = ?", (stream_id, pin_id)
    ).fetchone()
    if existing:
        # Promote to confirmed manual
        conn.execute(
            "UPDATE connections SET confirmed = TRUE, source = 'manual', llm_note = COALESCE(?, llm_note) WHERE id = ?",
            (note, existing["id"]),
        )
        return existing["id"]

    conn_id = _new_id()
    conn.execute(
        """
        INSERT INTO connections (id, stream_id, pin_id, similarity, llm_note, confirmed, source, created_at)
        VALUES (?, ?, ?, 1.0, ?, TRUE, 'manual', ?)
        """,
        (conn_id, stream_id, pin_id, note, now_utc()),
    )
    record(conn, "manual_link", stream_id=stream_id, pin_id=pin_id,
           metadata={"connection_id": conn_id})
    return conn_id
