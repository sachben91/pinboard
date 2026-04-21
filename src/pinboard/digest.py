"""Daily digest: Claude picks 3 streams per channel worth reading."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .scoring import lab_scores


def _candidate_streams(conn: sqlite3.Connection, channel_id: str, limit: int = 20) -> list[dict]:
    """Pool of candidates: mix of high lab scores and recent additions."""
    by_score = lab_scores(conn, channel_id=channel_id, half_life_days=14.0, limit=limit)

    # Also grab recently added streams not already in the score list
    score_ids = {r["id"] for r in by_score}
    recent = conn.execute(
        """
        SELECT s.id, s.title, s.kind, s.source, s.content_text, s.created_at
        FROM streams s
        WHERE s.channel_id = ?
          AND s.id NOT IN (SELECT stream_id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL)
          AND s.id NOT IN ({})
        ORDER BY s.created_at DESC
        LIMIT 10
        """.format(",".join("?" * len(score_ids)) if score_ids else "SELECT NULL"),
        [channel_id, channel_id] + list(score_ids),
    ).fetchall() if score_ids else conn.execute(
        """
        SELECT s.id, s.title, s.kind, s.source, s.content_text, s.created_at
        FROM streams s
        WHERE s.channel_id = ?
          AND s.id NOT IN (SELECT stream_id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL)
        ORDER BY s.created_at DESC LIMIT 10
        """,
        [channel_id, channel_id],
    ).fetchall()

    combined = list(by_score)
    seen = score_ids.copy()
    for r in recent:
        if r["id"] not in seen:
            combined.append({
                "id": r["id"], "title": r["title"], "kind": r["kind"],
                "source": r["source"] or "", "content_text": r["content_text"] or "",
                "score": 0.0, "open_count": 0,
            })
            seen.add(r["id"])
    return combined


def _pin_context(conn: sqlite3.Connection, channel_id: str) -> str:
    rows = conn.execute(
        """
        SELECT s.title, s.content_text, p.note
        FROM pins p JOIN streams s ON s.id = p.stream_id
        WHERE p.channel_id = ? AND p.unpinned_at IS NULL
        ORDER BY p.slot_order
        """,
        (channel_id,),
    ).fetchall()
    if not rows:
        return "No active pins."
    parts = []
    for r in rows:
        note = f" (pin note: {r['note']})" if r["note"] else ""
        excerpt = (r["content_text"] or "")[:300]
        parts.append(f"- {r['title']}{note}\n  {excerpt}")
    return "\n".join(parts)


def claude_pick(cfg, channel_name: str, pin_context: str, candidates: list[dict]) -> list[dict]:
    """Ask Claude to pick 3 streams and write a short note on each."""
    if not cfg.anthropic_api_key or not candidates:
        return candidates[:3]

    candidate_list = "\n".join(
        f"{i+1}. [{c['kind']}] {c['title']} (score={c['score']}, opens={c.get('open_count',0)})\n"
        f"   URL: {c.get('source','')}\n"
        f"   Excerpt: {(c.get('content_text') or '')[:200]}"
        for i, c in enumerate(candidates[:15])
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": (
                    f"You are curating a daily reading digest for the '{channel_name}' channel.\n\n"
                    f"The user's current focus (pinned items):\n{pin_context}\n\n"
                    f"Candidate streams to choose from:\n{candidate_list}\n\n"
                    "Pick the 3 most worth reading today given the user's current focus. "
                    "For each, write one sentence on why it's worth reading now.\n\n"
                    "Reply in this exact format for each pick:\n"
                    "PICK: <number from list>\n"
                    "WHY: <one sentence>\n"
                    "(repeat for all 3 picks)"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        picks = []
        for block in text.split("PICK:")[1:]:
            lines = block.strip().splitlines()
            try:
                idx = int(lines[0].strip()) - 1
                why = lines[1].replace("WHY:", "").strip() if len(lines) > 1 else ""
                if 0 <= idx < len(candidates):
                    entry = dict(candidates[idx])
                    entry["why"] = why
                    picks.append(entry)
            except (ValueError, IndexError):
                continue
        return picks[:3] if picks else candidates[:3]
    except Exception:
        return [dict(c) | {"why": ""} for c in candidates[:3]]


def build_digest(conn: sqlite3.Connection, cfg) -> list[dict]:
    """Build digest for all channels. Returns list of channel dicts with picks."""
    channels = conn.execute("SELECT id, name FROM channels ORDER BY created_at").fetchall()
    result = []
    for ch in channels:
        candidates = _candidate_streams(conn, ch["id"])
        if not candidates:
            continue
        pin_ctx = _pin_context(conn, ch["id"])
        picks = claude_pick(cfg, ch["name"], pin_ctx, candidates)
        result.append({
            "channel_id": ch["id"],
            "channel_name": ch["name"],
            "pin_context": pin_ctx,
            "picks": picks,
        })
    return result


def send_telegram(cfg, message: str) -> bool:
    """Send a message via Telegram bot. Returns True on success."""
    import urllib.request
    import urllib.parse
    token = cfg.extra.get("telegram_bot_token", "")
    chat_id = cfg.extra.get("telegram_chat_id", "")
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "false",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def render_telegram(digest: list[dict], date_str: str) -> str:
    """Render digest as Telegram HTML message."""
    lines = [f"📌 <b>Pinboard Daily Digest</b>\n{date_str}\n"]
    for ch in digest:
        lines.append(f"\n<b>#{ch['channel_name']}</b>")
        for i, p in enumerate(ch["picks"], 1):
            url = p.get("source") or ""
            title = p["title"]
            link = f'<a href="{url}">{title}</a>' if url else f"<b>{title}</b>"
            why = p.get("why", "")
            lines.append(f"\n{i}. {link}")
            if why:
                lines.append(f"<i>{why}</i>")
    return "\n".join(lines)


def render_html(digest: list[dict], date_str: str) -> str:
    """Render the digest as an HTML email."""
    sections = ""
    for ch in digest:
        picks_html = ""
        for p in ch["picks"]:
            url = p.get("source") or ""
            link = f'<a href="{url}">{p["title"]}</a>' if url else p["title"]
            why = p.get("why", "")
            picks_html += f"""
            <div style="margin-bottom:20px; padding:12px; background:#f9f9f9; border-left:3px solid #7c3aed;">
                <div style="font-size:15px; font-weight:600; margin-bottom:4px;">{link}</div>
                <div style="font-size:13px; color:#555; font-style:italic;">{why}</div>
                <div style="font-size:11px; color:#999; margin-top:4px;">{p.get('kind','').upper()}</div>
            </div>"""

        sections += f"""
        <div style="margin-bottom:36px;">
            <h2 style="font-size:16px; color:#7c3aed; border-bottom:1px solid #e5e7eb; padding-bottom:6px;">
                #{ch['channel_name']}
            </h2>
            {picks_html}
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family: -apple-system, sans-serif; max-width:600px; margin:0 auto; padding:24px; color:#111;">
    <h1 style="font-size:20px; margin-bottom:4px;">📌 Pinboard Daily Digest</h1>
    <p style="color:#888; font-size:13px; margin-bottom:32px;">{date_str}</p>
    {sections}
    <p style="color:#bbb; font-size:11px; margin-top:40px;">Sent by pinboard · your local knowledge system</p>
</body>
</html>"""


def render_plain(digest: list[dict], date_str: str) -> str:
    lines = [f"Pinboard Daily Digest — {date_str}\n"]
    for ch in digest:
        lines.append(f"\n#{ch['channel_name']}")
        lines.append("-" * 40)
        for i, p in enumerate(ch["picks"], 1):
            lines.append(f"{i}. {p['title']}")
            if p.get("source"):
                lines.append(f"   {p['source']}")
            if p.get("why"):
                lines.append(f"   → {p['why']}")
    return "\n".join(lines)
