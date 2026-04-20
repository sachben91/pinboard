"""Connection detection between streams and pins — Claude-first, embedding fallback."""

from __future__ import annotations

import sqlite3
import uuid

from .embeddings import deserialize, cosine_similarity
from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


def _claude_judge(cfg, stream_excerpt: str, pin_excerpt: str) -> tuple[bool, str | None]:
    """Ask Claude if two passages are conceptually connected.
    Returns (is_connected, explanation)."""
    if not cfg.anthropic_api_key:
        return False, None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    "You are helping a person manage their personal knowledge.\n\n"
                    "They have pinned the following as a current focus:\n"
                    f"PIN:\n{pin_excerpt[:800]}\n\n"
                    "They just captured this new item:\n"
                    f"STREAM:\n{stream_excerpt[:800]}\n\n"
                    "Are these conceptually connected in any meaningful way — "
                    "even if the surface topics seem different? Think about underlying themes, "
                    "questions, tensions, or ideas that both might be exploring.\n\n"
                    "Reply with:\n"
                    "CONNECTED: yes or no\n"
                    "REASON: one or two sentences explaining why (or why not)"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        connected = "yes" in text.split("CONNECTED:")[-1].split("\n")[0].lower()
        reason = None
        if "REASON:" in text:
            reason = text.split("REASON:")[-1].strip()
        return connected, reason
    except Exception:
        return False, None


def _embedding_fallback(cfg, stream_vec, pin_vec) -> tuple[bool, None]:
    """Fall back to cosine similarity if no Anthropic key."""
    sim = cosine_similarity(stream_vec, pin_vec)
    return sim >= cfg.connection_threshold, None


def auto_suggest(conn: sqlite3.Connection, stream_id: str, cfg) -> list[str]:
    """Check new stream against active pins using Claude (or embedding fallback).
    Returns list of created connection ids."""
    stream = conn.execute(
        "SELECT embedding, content_text FROM streams WHERE id = ?", (stream_id,)
    ).fetchone()
    if not stream or not stream["content_text"]:
        return []

    stream_excerpt = (stream["content_text"] or "")[:800]
    stream_vec = deserialize(stream["embedding"]) if stream["embedding"] else None

    active_pins = conn.execute(
        """
        SELECT p.id as pin_id, s.embedding, s.content_text
        FROM pins p
        JOIN streams s ON s.id = p.stream_id
        WHERE p.unpinned_at IS NULL
        """
    ).fetchall()

    created = []
    for pin_row in active_pins:
        # Skip if connection already exists
        existing = conn.execute(
            "SELECT id FROM connections WHERE stream_id = ? AND pin_id = ?",
            (stream_id, pin_row["pin_id"]),
        ).fetchone()
        if existing:
            continue

        pin_excerpt = (pin_row["content_text"] or "")[:800]

        # Use Claude if available, otherwise fall back to embeddings
        if cfg.anthropic_api_key:
            connected, note = _claude_judge(cfg, stream_excerpt, pin_excerpt)
            sim = cosine_similarity(
                deserialize(stream["embedding"]), deserialize(pin_row["embedding"])
            ) if stream["embedding"] and pin_row["embedding"] else 0.0
        elif stream_vec is not None and pin_row["embedding"]:
            pin_vec = deserialize(pin_row["embedding"])
            connected, note = _embedding_fallback(cfg, stream_vec, pin_vec)
            sim = cosine_similarity(stream_vec, pin_vec)
        else:
            continue

        if not connected:
            continue

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
