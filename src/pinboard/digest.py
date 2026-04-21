"""Daily digest: 1 pick from your streams + 2 web discoveries related to it."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone

from .scoring import lab_scores


# ---------------------------------------------------------------------------
# Stream pick
# ---------------------------------------------------------------------------

def _pin_context(conn: sqlite3.Connection, channel_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT s.title, s.content_text, p.note
        FROM pins p JOIN streams s ON s.id = p.stream_id
        WHERE p.channel_id = ? AND p.unpinned_at IS NULL
        ORDER BY p.slot_order
        """,
        (channel_id,),
    ).fetchall()
    return [{"title": r["title"], "excerpt": (r["content_text"] or "")[:400], "note": r["note"]} for r in rows]


def _candidate_streams(conn: sqlite3.Connection, channel_id: str) -> list[dict]:
    """Unread streams ranked by lab score + recency."""
    by_score = lab_scores(conn, channel_id=channel_id, half_life_days=14.0, limit=20)
    score_ids = {r["id"] for r in by_score}

    recent_rows = conn.execute(
        """
        SELECT s.id, s.title, s.kind, s.source, s.content_text, s.created_at
        FROM streams s
        WHERE s.channel_id = ?
          AND s.id NOT IN (SELECT stream_id FROM pins WHERE channel_id = ? AND unpinned_at IS NULL)
        ORDER BY s.created_at DESC LIMIT 10
        """,
        (channel_id, channel_id),
    ).fetchall()

    combined = list(by_score)
    seen = set(score_ids)
    for r in recent_rows:
        if r["id"] not in seen:
            combined.append({
                "id": r["id"], "title": r["title"], "kind": r["kind"],
                "source": r["source"] or "", "content_text": r["content_text"] or "",
                "score": 0.0, "open_count": 0,
            })
            seen.add(r["id"])
    return combined


def pick_stream(cfg, channel_name: str, pins: list[dict], candidates: list[dict]) -> dict | None:
    """Ask Claude to pick the single most relevant stream from the candidates."""
    if not candidates:
        return None
    if not cfg.anthropic_api_key:
        return candidates[0]

    pin_ctx = "\n".join(f"- {p['title']}: {p['excerpt'][:200]}" for p in pins) or "No active pins."
    cand_list = "\n".join(
        f"{i+1}. {c['title']} (score={c['score']})\n   {(c.get('content_text') or '')[:200]}"
        for i, c in enumerate(candidates[:12])
    )

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=200,
            messages=[{
                "role": "user",
                "content": (
                    f"Channel: {channel_name}\n"
                    f"User's current pins (focus areas):\n{pin_ctx}\n\n"
                    f"Candidate streams:\n{cand_list}\n\n"
                    "Pick the single most interesting unread stream given the user's focus. "
                    "Reply:\nPICK: <number>\nWHY: <one sentence>"
                ),
            }],
        )
        text = msg.content[0].text.strip()
        idx = int(text.split("PICK:")[-1].split("\n")[0].strip()) - 1
        why = text.split("WHY:")[-1].strip() if "WHY:" in text else ""
        if 0 <= idx < len(candidates):
            return dict(candidates[idx]) | {"why": why}
    except Exception:
        pass
    return dict(candidates[0]) | {"why": ""}


# ---------------------------------------------------------------------------
# Web discovery
# ---------------------------------------------------------------------------

def _search_queries(cfg, stream: dict, pins: list[dict]) -> list[str]:
    """Ask Claude for 2 targeted search queries based on the picked stream."""
    if not cfg.anthropic_api_key:
        return [stream["title"]]

    pin_titles = ", ".join(p["title"] for p in pins) or "none"
    excerpt = (stream.get("content_text") or "")[:400]

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": (
                    f"A reader is focused on: {pin_titles}\n\n"
                    f"They're reading: {stream['title']}\n{excerpt}\n\n"
                    "Write 2 web search queries to find related articles or essays "
                    "they haven't seen yet. Be specific — target the underlying themes, "
                    "not just the surface topic.\n\n"
                    "Reply with exactly 2 lines, one query per line, no numbering."
                ),
            }],
        )
        queries = [q.strip() for q in msg.content[0].text.strip().splitlines() if q.strip()]
        return queries[:2]
    except Exception:
        return [stream["title"]]


def _best_signals(cfg, stream: dict, signals: list[str]) -> list[str]:
    """Ask Claude to pick the 2 most relevant search signals for a stream."""
    if not cfg.anthropic_api_key or len(signals) <= 2:
        return signals[:2]
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        signals_txt = "\n".join(f"{i+1}. {s}" for i, s in enumerate(signals))
        msg = client.messages.create(
            model=cfg.llm_model,
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": (
                    f"Stream to find related reading for: {stream['title']}\n"
                    f"Excerpt: {(stream.get('content_text') or '')[:300]}\n\n"
                    f"Available search signals:\n{signals_txt}\n\n"
                    "Pick the 2 most relevant signals. Reply with just two line numbers, e.g.:\n3\n5"
                ),
            }],
        )
        idxs = [int(x.strip()) - 1 for x in msg.content[0].text.strip().splitlines() if x.strip().isdigit()]
        return [signals[i] for i in idxs if 0 <= i < len(signals)][:2]
    except Exception:
        return signals[:2]


def _web_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        from ddgs import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        return [{"title": r.get("title",""), "url": r.get("href",""), "snippet": r.get("body","")} for r in results]
    except Exception:
        return []


def find_web_picks(cfg, stream: dict, pins: list[dict], skills: list[dict] | None = None) -> list[dict]:
    """Run 2 searches and ask Claude to pick the best result from each."""
    # Prefer skill-derived search signals over generic queries
    if skills:
        from .skills import all_search_signals
        signals = all_search_signals(skills)
        # Pick the 2 most relevant signals for this stream by asking Claude
        queries = _best_signals(cfg, stream, signals) if signals else _search_queries(cfg, stream, pins)
    else:
        queries = _search_queries(cfg, stream, pins)
    picks = []

    for query in queries[:2]:
        results = _web_search(query)
        if not results:
            continue

        if not cfg.anthropic_api_key:
            picks.append(dict(results[0]) | {"why": ""})
            continue

        results_txt = "\n".join(
            f"{i+1}. {r['title']}\n   {r['url']}\n   {r['snippet'][:200]}"
            for i, r in enumerate(results)
        )
        pin_titles = ", ".join(p["title"] for p in pins) or "none"

        try:
            import anthropic
            client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
            msg = client.messages.create(
                model=cfg.llm_model,
                max_tokens=150,
                messages=[{
                    "role": "user",
                    "content": (
                        f"User focus: {pin_titles}\n"
                        f"They're reading: {stream['title']}\n\n"
                        f"Search results for '{query}':\n{results_txt}\n\n"
                        "Pick the most intellectually interesting result. "
                        "Reply:\nPICK: <number>\nWHY: <one sentence>"
                    ),
                }],
            )
            text = msg.content[0].text.strip()
            idx = int(text.split("PICK:")[-1].split("\n")[0].strip()) - 1
            why = text.split("WHY:")[-1].strip() if "WHY:" in text else ""
            if 0 <= idx < len(results):
                picks.append(dict(results[idx]) | {"why": why})
        except Exception:
            picks.append(dict(results[0]) | {"why": ""})

    return picks


# ---------------------------------------------------------------------------
# Build + render
# ---------------------------------------------------------------------------

def build_digest(conn: sqlite3.Connection, cfg) -> list[dict]:
    """Build 1+2 digest for all channels."""
    from .skills import get_skills_for_channel
    channels = conn.execute("SELECT id, name FROM channels ORDER BY created_at").fetchall()
    result = []
    for ch in channels:
        pins = _pin_context(conn, ch["id"])
        skills = get_skills_for_channel(conn, ch["id"])
        candidates = _candidate_streams(conn, ch["id"])
        stream_pick = pick_stream(cfg, ch["name"], pins, candidates)
        if not stream_pick:
            continue
        web_picks = find_web_picks(cfg, stream_pick, pins, skills=skills or None)
        result.append({
            "channel_id": ch["id"],
            "channel_name": ch["name"],
            "stream_pick": stream_pick,
            "web_picks": web_picks,
            "skills_used": len(skills),
        })
    return result


def _send_telegram_raw(cfg, message: str) -> bool:
    import urllib.request, urllib.parse
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


def send_telegram(cfg, digest: list[dict], date_str: str) -> bool:
    """Send one Telegram message per channel, plus a header message."""
    # Header
    header = f"📌 <b>Pinboard Daily Digest</b> — {date_str}\n{len(digest)} channel(s)"
    if not _send_telegram_raw(cfg, header):
        return False

    for ch in digest:
        msg = _render_channel_telegram(ch)
        if not _send_telegram_raw(cfg, msg):
            return False
    return True


def _render_channel_telegram(ch: dict) -> str:
    divider = "─" * 20
    lines = [
        f"{divider}",
        f"<b>#{ch['channel_name'].upper()}</b>",
        f"{divider}",
        "",
        "📖 <b>From your streams</b>",
    ]

    sp = ch["stream_pick"]
    url = sp.get("source") or ""
    link = f'<a href="{url}">{sp["title"]}</a>' if url else f'<b>{sp["title"]}</b>'
    lines.append(link)
    if sp.get("why"):
        lines.append(f"<i>{sp['why']}</i>")

    if ch.get("web_picks"):
        lines.append("")
        lines.append("🌐 <b>Discovered for you</b>")
        for wp in ch["web_picks"]:
            wurl = wp.get("url") or ""
            wlink = f'<a href="{wurl}">{wp["title"]}</a>' if wurl else wp["title"]
            lines.append(wlink)
            if wp.get("why"):
                lines.append(f"<i>{wp['why']}</i>")
            lines.append("")

    if ch.get("skills_used", 0) > 0:
        lines.append(f"<i>Curated using {ch['skills_used']} pin skill(s)</i>")

    return "\n".join(lines)


def render_plain(digest: list[dict], date_str: str) -> str:
    lines = [f"Pinboard Daily Digest — {date_str}", "=" * 50]
    for ch in digest:
        lines.append(f"\n{'─'*20} #{ch['channel_name'].upper()} {'─'*20}")

        sp = ch["stream_pick"]
        lines.append(f"\n📖 FROM YOUR STREAMS")
        lines.append(f"   {sp['title']}")
        if sp.get("source"):
            lines.append(f"   {sp['source']}")
        if sp.get("why"):
            lines.append(f"   → {sp['why']}")

        if ch.get("web_picks"):
            lines.append(f"\n🌐 DISCOVERED FOR YOU")
            for wp in ch["web_picks"]:
                lines.append(f"   {wp['title']}")
                if wp.get("url"):
                    lines.append(f"   {wp['url']}")
                if wp.get("why"):
                    lines.append(f"   → {wp['why']}")
                lines.append("")

        if ch.get("skills_used", 0) > 0:
            lines.append(f"   [Curated using {ch['skills_used']} pin skill(s)]")

    return "\n".join(lines)
