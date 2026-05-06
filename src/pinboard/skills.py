"""Pin skill generation and retrieval."""

from __future__ import annotations

import json
import sqlite3
import uuid

from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


def generate_skill(cfg, pin_id: str, title: str, content_text: str) -> dict | None:
    """Call Claude to decompose a pin cluster's content. Returns skill dict or None."""
    if not cfg.anthropic_api_key or not content_text:
        return None
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    f"A reader has pinned this as a current focus:\n\n"
                    f"Title: {title}\n\n"
                    f"Excerpt:\n{content_text[:1500]}\n\n"
                    "Decompose this into its intellectual sub-components. "
                    "Be specific and generative — the goal is to use these to find new things to read.\n\n"
                    "Reply in this exact JSON format:\n"
                    "{\n"
                    '  "themes": ["3-5 core themes or concepts"],\n'
                    '  "questions": ["2-3 key questions the piece is exploring or raising"],\n'
                    '  "adjacent": ["4-6 adjacent concepts, fields, or ideas worth exploring"],\n'
                    '  "search_signals": ["3-5 specific search queries to find related reading"]\n'
                    "}\n\n"
                    "Only return the JSON, nothing else."
                ),
            }],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception:
        return None


def save_skill(conn: sqlite3.Connection, pin_id: str, skill: dict) -> str:
    skill_id = _new_id()
    conn.execute(
        """
        INSERT INTO pin_skills (id, pin_id, themes, questions, adjacent, search_signals, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            skill_id, pin_id,
            json.dumps(skill.get("themes", [])),
            json.dumps(skill.get("questions", [])),
            json.dumps(skill.get("adjacent", [])),
            json.dumps(skill.get("search_signals", [])),
            now_utc(),
        ),
    )
    record(conn, "skill_generated", pin_id=pin_id, metadata={"skill_id": skill_id})
    return skill_id


def get_skills_for_stream(conn: sqlite3.Connection, stream_id: str) -> list[dict]:
    """Return all skills for active pin clusters in a stream."""
    rows = conn.execute(
        """
        SELECT ps.*, p.name as pin_name
        FROM pin_skills ps
        JOIN pins p ON p.id = ps.pin_id
        WHERE p.stream_id = ? AND p.closed_at IS NULL
        ORDER BY p.slot_order
        """,
        (stream_id,),
    ).fetchall()
    return [{
        "pin_id": r["pin_id"],
        "pin_title": r["pin_name"],
        "themes": json.loads(r["themes"]),
        "questions": json.loads(r["questions"]),
        "adjacent": json.loads(r["adjacent"]),
        "search_signals": json.loads(r["search_signals"]),
    } for r in rows]


def all_search_signals(skills: list[dict]) -> list[str]:
    signals = []
    for s in skills:
        signals.extend(s.get("search_signals", []))
    return signals
