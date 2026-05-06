"""Connection detection between links and pin clusters — Claude-first, embedding fallback."""

from __future__ import annotations

import sqlite3
import uuid

from .embeddings import deserialize, cosine_similarity
from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


def _claude_judge(cfg, link_excerpt: str, pin_excerpt: str) -> tuple[bool, str | None]:
    """Ask Claude if a link and a pin cluster context are conceptually connected."""
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
                    f"LINK:\n{link_excerpt[:800]}\n\n"
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


def _embedding_fallback(cfg, link_vec, pin_vec) -> tuple[bool, None]:
    sim = cosine_similarity(link_vec, pin_vec)
    return sim >= cfg.connection_threshold, None


def _pin_aggregate(conn: sqlite3.Connection, pin_id: str) -> tuple[str, bytes | None]:
    """Return (excerpt, avg_embedding) from all links in a pin cluster."""
    import numpy as np
    rows = conn.execute(
        """
        SELECT l.content_text, l.embedding
        FROM pin_links pl JOIN links l ON l.id = pl.link_id
        WHERE pl.pin_id = ?
        ORDER BY pl.added_at
        """,
        (pin_id,),
    ).fetchall()
    if not rows:
        return "", None

    n = len(rows)
    per_link = max(1, 800 // n)
    excerpt = "\n---\n".join((r["content_text"] or "")[:per_link] for r in rows if r["content_text"])

    vecs = [deserialize(r["embedding"]) for r in rows if r["embedding"]]
    if not vecs:
        return excerpt, None
    avg = np.mean(vecs, axis=0)
    from .embeddings import serialize
    return excerpt, serialize(avg)


def prepare_auto_connections(conn: sqlite3.Connection, link_id: str, cfg, stream_id: str) -> list[dict]:
    """Read pin context from DB and call Claude/embeddings. No DB writes — safe to call in parallel.
    Returns list of connection dicts ready to pass to write_auto_connections."""
    link = conn.execute(
        "SELECT embedding, content_text FROM links WHERE id = ?", (link_id,)
    ).fetchone()
    if not link or not link["content_text"]:
        return []

    link_excerpt = (link["content_text"] or "")[:800]
    link_vec = deserialize(link["embedding"]) if link["embedding"] else None

    active_pins = conn.execute(
        "SELECT id as pin_id FROM pins WHERE stream_id = ? AND closed_at IS NULL",
        (stream_id,),
    ).fetchall()

    existing_pins = {
        row["pin_id"]
        for row in conn.execute(
            "SELECT pin_id FROM connections WHERE link_id = ?", (link_id,)
        ).fetchall()
    }

    pending = []
    for pin_row in active_pins:
        pin_id = pin_row["pin_id"]
        if pin_id in existing_pins:
            continue

        pin_excerpt, pin_emb_blob = _pin_aggregate(conn, pin_id)
        if not pin_excerpt:
            continue

        if cfg.anthropic_api_key:
            connected, note = _claude_judge(cfg, link_excerpt, pin_excerpt)
            pin_vec = deserialize(pin_emb_blob) if pin_emb_blob else None
            sim = cosine_similarity(link_vec, pin_vec) if link_vec is not None and pin_vec is not None else 0.0
        elif link_vec is not None and pin_emb_blob:
            pin_vec = deserialize(pin_emb_blob)
            connected, note = _embedding_fallback(cfg, link_vec, pin_vec)
            sim = cosine_similarity(link_vec, pin_vec)
        else:
            continue

        if not connected:
            continue

        pending.append({
            "conn_id": _new_id(),
            "link_id": link_id,
            "pin_id": pin_id,
            "similarity": sim,
            "note": note,
        })
    return pending


def write_auto_connections(conn: sqlite3.Connection, pending: list[dict]) -> list[str]:
    """Write prepared connection dicts to the DB. Returns list of created connection ids."""
    created = []
    for c in pending:
        conn.execute(
            """
            INSERT OR IGNORE INTO connections (id, link_id, pin_id, similarity, llm_note, confirmed, source, created_at)
            VALUES (?, ?, ?, ?, ?, FALSE, 'auto', ?)
            """,
            (c["conn_id"], c["link_id"], c["pin_id"], c["similarity"], c["note"], now_utc()),
        )
        record(
            conn, "suggest_connection",
            stream_id=c["link_id"], pin_id=c["pin_id"],
            metadata={"similarity": c["similarity"], "connection_id": c["conn_id"]},
        )
        created.append(c["conn_id"])
    return created


def auto_suggest(conn: sqlite3.Connection, link_id: str, cfg, stream_id: str) -> list[str]:
    """Check new link against active pin clusters. Returns list of created connection ids."""
    pending = prepare_auto_connections(conn, link_id, cfg, stream_id)
    return write_auto_connections(conn, pending)


def confirm_connection(conn: sqlite3.Connection, conn_id: str) -> None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row:
        raise ValueError(f"Connection {conn_id} not found.")
    conn.execute("UPDATE connections SET confirmed = TRUE WHERE id = ?", (conn_id,))
    record(conn, "confirm_connection", stream_id=row["link_id"], pin_id=row["pin_id"],
           metadata={"connection_id": conn_id})


def reject_connection(conn: sqlite3.Connection, conn_id: str) -> None:
    row = conn.execute("SELECT * FROM connections WHERE id = ?", (conn_id,)).fetchone()
    if not row:
        raise ValueError(f"Connection {conn_id} not found.")
    conn.execute("DELETE FROM connections WHERE id = ?", (conn_id,))
    record(conn, "reject_connection", stream_id=row["link_id"], pin_id=row["pin_id"],
           metadata={"connection_id": conn_id})


def manual_link(
    conn: sqlite3.Connection, link_id: str, pin_id: str, note: str | None = None
) -> str:
    link = conn.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone()
    if not link:
        raise ValueError(f"Link {link_id} not found.")
    pin = conn.execute("SELECT id FROM pins WHERE id = ?", (pin_id,)).fetchone()
    if not pin:
        raise ValueError(f"Pin {pin_id} not found.")

    existing = conn.execute(
        "SELECT id FROM connections WHERE link_id = ? AND pin_id = ?", (link_id, pin_id)
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
        INSERT INTO connections (id, link_id, pin_id, similarity, llm_note, confirmed, source, created_at)
        VALUES (?, ?, ?, 1.0, ?, TRUE, 'manual', ?)
        """,
        (conn_id, link_id, pin_id, note, now_utc()),
    )
    record(conn, "manual_link", stream_id=link_id, pin_id=pin_id,
           metadata={"connection_id": conn_id})
    return conn_id
