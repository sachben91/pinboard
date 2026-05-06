"""Daily digest: 1 pick from your links + 2 web discoveries related to it."""

from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timezone

from .scoring import lab_scores


def _pin_context(conn: sqlite3.Connection, stream_id: str) -> list[dict]:
    """Build pin context from all active clusters, aggregating their links."""
    pins = conn.execute(
        "SELECT id, name, note FROM pins WHERE stream_id = ? AND closed_at IS NULL ORDER BY slot_order",
        (stream_id,),
    ).fetchall()
    result = []
    for p in pins:
        links = conn.execute(
            """
            SELECT l.title, l.content_text FROM pin_links pl
            JOIN links l ON l.id = pl.link_id
            WHERE pl.pin_id = ? ORDER BY pl.added_at
            """,
            (p["id"],),
        ).fetchall()
        excerpt = " / ".join((r["content_text"] or "")[:200] for r in links if r["content_text"])
        titles = ", ".join(r["title"] for r in links) if links else p["name"]
        result.append({"title": p["name"] or titles, "excerpt": excerpt[:400], "note": p["note"]})
    return result


def _candidate_links(conn: sqlite3.Connection, stream_id: str) -> list[dict]:
    """Unread links ranked by lab score + recency."""
    by_score = lab_scores(conn, stream_id=stream_id, half_life_days=14.0, limit=20)
    score_ids = {r["id"] for r in by_score}

    recent_rows = conn.execute(
        """
        SELECT l.id, l.title, l.kind, l.source, l.content_text, l.created_at
        FROM links l
        WHERE l.stream_id = ?
          AND l.id NOT IN (
              SELECT pl.link_id FROM pin_links pl
              JOIN pins p ON p.id = pl.pin_id
              WHERE p.stream_id = ? AND p.closed_at IS NULL
          )
        ORDER BY l.created_at DESC LIMIT 10
        """,
        (stream_id, stream_id),
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


def pick_link(cfg, stream_name: str, pins: list[dict], candidates: list[dict]) -> dict | None:
    """Ask Claude to pick the single most relevant link from the candidates."""
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
                    f"Stream: {stream_name}\n"
                    f"User's current pins (focus areas):\n{pin_ctx}\n\n"
                    f"Candidate links:\n{cand_list}\n\n"
                    "Pick the single most interesting unread link given the user's focus. "
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


def _search_queries(cfg, link: dict, pins: list[dict]) -> list[str]:
    if not cfg.anthropic_api_key:
        return [link["title"]]

    pin_titles = ", ".join(p["title"] for p in pins) or "none"
    excerpt = (link.get("content_text") or "")[:400]

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
                    f"They're reading: {link['title']}\n{excerpt}\n\n"
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
        return [link["title"]]


def _best_signals(cfg, link: dict, signals: list[str]) -> list[str]:
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
                    f"Link to find related reading for: {link['title']}\n"
                    f"Excerpt: {(link.get('content_text') or '')[:300]}\n\n"
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
        return [{"title": r.get("title", ""), "url": r.get("href", ""), "snippet": r.get("body", "")} for r in results]
    except Exception:
        return []


def find_web_picks(cfg, link: dict, pins: list[dict], skills: list[dict] | None = None) -> list[dict]:
    if skills:
        from .skills import all_search_signals
        signals = all_search_signals(skills)
        queries = _best_signals(cfg, link, signals) if signals else _search_queries(cfg, link, pins)
    else:
        queries = _search_queries(cfg, link, pins)
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
                        f"They're reading: {link['title']}\n\n"
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


def build_digest(conn: sqlite3.Connection, cfg) -> list[dict]:
    """Build 1+2 digest for all streams."""
    from .skills import get_skills_for_stream
    streams = conn.execute("SELECT id, name FROM streams ORDER BY created_at").fetchall()
    result = []
    for s in streams:
        pins = _pin_context(conn, s["id"])
        skills = get_skills_for_stream(conn, s["id"])
        candidates = _candidate_links(conn, s["id"])
        link_pick = pick_link(cfg, s["name"], pins, candidates)
        if not link_pick:
            continue
        web_picks = find_web_picks(cfg, link_pick, pins, skills=skills or None)
        result.append({
            "stream_id": s["id"],
            "stream_name": s["name"],
            "link_pick": link_pick,
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
    header = f"📌 <b>Pinboard Daily Digest</b> — {date_str}\n{len(digest)} stream(s)"
    if not _send_telegram_raw(cfg, header):
        return False
    for s in digest:
        msg = _render_stream_telegram(s)
        if not _send_telegram_raw(cfg, msg):
            return False
    return True


def _render_stream_telegram(s: dict) -> str:
    divider = "─" * 20
    lines = [
        divider,
        f"<b>#{s['stream_name'].upper()}</b>",
        divider,
        "",
        "📖 <b>From your links</b>",
    ]
    lp = s["link_pick"]
    url = lp.get("source") or ""
    link = f'<a href="{url}">{lp["title"]}</a>' if url else f'<b>{lp["title"]}</b>'
    lines.append(link)
    if lp.get("why"):
        lines.append(f"<i>{lp['why']}</i>")

    if s.get("web_picks"):
        lines += ["", "🌐 <b>Discovered for you</b>"]
        for wp in s["web_picks"]:
            wurl = wp.get("url") or ""
            wlink = f'<a href="{wurl}">{wp["title"]}</a>' if wurl else wp["title"]
            lines.append(wlink)
            if wp.get("why"):
                lines.append(f"<i>{wp['why']}</i>")
            lines.append("")

    if s.get("skills_used", 0) > 0:
        lines.append(f"<i>Curated using {s['skills_used']} pin skill(s)</i>")

    return "\n".join(lines)


def render_plain(digest: list[dict], date_str: str) -> str:
    lines = [f"Pinboard Daily Digest — {date_str}", "=" * 50]
    for s in digest:
        lines.append(f"\n{'─'*20} #{s['stream_name'].upper()} {'─'*20}")
        lp = s["link_pick"]
        lines.append("\n📖 FROM YOUR LINKS")
        lines.append(f"   {lp['title']}")
        if lp.get("source"):
            lines.append(f"   {lp['source']}")
        if lp.get("why"):
            lines.append(f"   → {lp['why']}")
        if s.get("web_picks"):
            lines.append("\n🌐 DISCOVERED FOR YOU")
            for wp in s["web_picks"]:
                lines.append(f"   {wp['title']}")
                if wp.get("url"):
                    lines.append(f"   {wp['url']}")
                if wp.get("why"):
                    lines.append(f"   → {wp['why']}")
                lines.append("")
        if s.get("skills_used", 0) > 0:
            lines.append(f"   [Curated using {s['skills_used']} pin skill(s)]")
    return "\n".join(lines)
