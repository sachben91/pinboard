#!/usr/bin/env python3
"""
YCPin Discord bot — spaced repetition review for Pinboard links.

Posts a random link from a Pinboard stream into a Discord channel.
Users react with ✅ to keep or ❌ to discard (removes from Pinboard).
Invoke manually with /ycpin slash command.
Scheduled to post automatically once per day.

Usage:
    DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=... python3 scripts/ycpin_bot.py

Environment variables:
    DISCORD_BOT_TOKEN   — required
    DISCORD_CHANNEL_ID  — channel to post in (default: 709454740614021121)
    YCPIN_THREAD_ID     — if set, post into this thread instead of the channel,
                          keeping the parent channel uncrowded
    YCPIN_STREAM        — Pinboard stream name (default: Governance Studies)
    YCPIN_HOUR          — UTC hour to post daily (default: 9)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import anthropic
import discord
from discord import app_commands
from discord.ext import tasks

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinboard.config import DB_PATH
from pinboard.db import get_conn, init_db
from pinboard.events import now_utc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ycpin")

# ── Config ────────────────────────────────────────────────────────────────

TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", "709454740614021121"))
_THREAD_ID_RAW = os.environ.get("YCPIN_THREAD_ID", "").strip()
THREAD_ID = int(_THREAD_ID_RAW) if _THREAD_ID_RAW else None
STREAM_NAME = os.environ.get("YCPIN_STREAM", "Governance Studies")
POST_HOUR_CENTRAL = int(os.environ.get("YCPIN_HOUR", "9"))
_CENTRAL = ZoneInfo("America/Chicago")

KEEP_EMOJI = "⬆️"
DISCARD_EMOJI = "⬇️"
# Discord sometimes strips the variation selector (U+FE0F) from arrow emojis
_KEEP_VARIANTS = {KEEP_EMOJI, "⬆"}
_DISCARD_VARIANTS = {DISCARD_EMOJI, "⬇"}

# Slot colors matching the web UI
EMBED_COLOR = 0x6366F1  # indigo

# ── DB helpers ────────────────────────────────────────────────────────────

def _get_stream_id(stream_name: str) -> str | None:
    with get_conn(DB_PATH) as db:
        row = db.execute(
            "SELECT id FROM streams WHERE name = ?", (stream_name,)
        ).fetchone()
        return row["id"] if row else None


def _pick_link(stream_id: str) -> dict | None:
    """
    Pick the next link for review using simple spaced repetition:
    - Never-reviewed links come first
    - Kept links become eligible again after 2^n days (n = keep count, max 32 days)
    - Discarded links are excluded (they'll be deleted anyway)
    """
    import random

    with get_conn(DB_PATH) as db:
        rows = db.execute(
            """
            WITH stats AS (
                SELECT link_id,
                       SUM(CASE WHEN outcome = 'kept' THEN 1 ELSE 0 END) AS keep_count,
                       MAX(posted_at) AS last_sent
                FROM discord_reviews
                WHERE stream_id = ?
                GROUP BY link_id
            )
            SELECT l.id, l.title, l.source, l.content_text, l.posted_at, l.tags,
                   COALESCE(s.keep_count, 0) AS keep_count,
                   s.last_sent
            FROM links l
            LEFT JOIN stats s ON s.link_id = l.id
            WHERE l.stream_id = ?
              AND 1=1
            """,
            (stream_id, stream_id),
        ).fetchall()

    # Filter by SRS interval in Python (avoids SQLite vs PostgreSQL date function differences)
    now = datetime.now(timezone.utc)
    eligible = []
    for row in rows:
        last_sent = row["last_sent"]
        if last_sent is None:
            eligible.append(row)
            continue
        if isinstance(last_sent, str):
            try:
                ls = datetime.fromisoformat(last_sent.replace("Z", "+00:00"))
                if ls.tzinfo is None:
                    ls = ls.replace(tzinfo=timezone.utc)
            except Exception:
                eligible.append(row)
                continue
        else:
            ls = last_sent if last_sent.tzinfo else last_sent.replace(tzinfo=timezone.utc)
        keep_count = row["keep_count"] or 0
        if (now - ls) >= timedelta(days=min(2 ** keep_count, 32)):
            eligible.append(row)

    if not eligible:
        return None

    never = [r for r in eligible if r["last_sent"] is None]
    pool = (never or eligible)[:5]
    row = random.choice(pool)
    tags = json.loads(row["tags"]) if row["tags"] else []
    text = row["content_text"] or ""
    summary = (text[:400].strip() + "…") if len(text) > 400 else text.strip()

    return {
        "id": row["id"],
        "title": row["title"],
        "source": row["source"],
        "summary": summary,
        "posted_at": row["posted_at"],
        "tags": tags,
        "keep_count": row["keep_count"],
    }


def _record_review(message_id: str, link_id: str, channel_id: int, stream_id: str) -> None:
    with get_conn(DB_PATH) as db:
        db.execute(
            """
            INSERT OR IGNORE INTO discord_reviews
                (message_id, link_id, channel_id, stream_id, posted_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (str(message_id), link_id, str(channel_id), stream_id, now_utc()),
        )


def _resolve_review(message_id: str, outcome: str) -> str | None:
    """Mark a review as resolved. Returns link_id or None if not found."""
    with get_conn(DB_PATH) as db:
        row = db.execute(
            "SELECT link_id, outcome FROM discord_reviews WHERE message_id = ?",
            (str(message_id),),
        ).fetchone()
        if not row or row["outcome"] is not None:
            return None  # not our message, or already resolved
        db.execute(
            "UPDATE discord_reviews SET outcome = ?, resolved_at = ? WHERE message_id = ?",
            (outcome, now_utc(), str(message_id)),
        )
        return row["link_id"]


def _delete_link(link_id: str) -> None:
    with get_conn(DB_PATH) as db:
        db.execute("DELETE FROM pin_links WHERE link_id = ?", (link_id,))
        db.execute("DELETE FROM connections WHERE link_id = ?", (link_id,))
        db.execute("DELETE FROM discord_reviews WHERE link_id = ?", (link_id,))
        db.execute("DELETE FROM links WHERE id = ?", (link_id,))


def _review_stats(stream_id: str) -> dict:
    with get_conn(DB_PATH) as db:
        total = db.execute(
            "SELECT COUNT(*) FROM links WHERE stream_id = ?", (stream_id,)
        ).fetchone()[0]
        kept = db.execute(
            "SELECT COUNT(DISTINCT link_id) FROM discord_reviews WHERE stream_id = ? AND outcome = 'kept'",
            (stream_id,),
        ).fetchone()[0]
        discarded = db.execute(
            "SELECT COUNT(*) FROM discord_reviews WHERE stream_id = ? AND outcome = 'discarded'",
            (stream_id,),
        ).fetchone()[0]
        unreviewed = db.execute(
            """
            SELECT COUNT(*) FROM links l
            WHERE l.stream_id = ?
              AND l.id NOT IN (SELECT link_id FROM discord_reviews WHERE stream_id = ?)
            """,
            (stream_id, stream_id),
        ).fetchone()[0]
        return {"total": total, "kept": kept, "discarded": discarded, "unreviewed": unreviewed}


# ── Bot ───────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.reactions = True
intents.message_content = True


class YCPinBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.stream_id: str | None = None

    async def setup_hook(self):
        await self.tree.sync()
        log.info("Slash commands synced")

    async def on_ready(self):
        self.stream_id = _get_stream_id(STREAM_NAME)
        if not self.stream_id:
            log.error(f"Stream '{STREAM_NAME}' not found in Pinboard DB.")
        else:
            log.info(f"YCPin ready — stream: {STREAM_NAME} ({self.stream_id})")
            if not self.daily_post.is_running():
                self.daily_post.start()

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.user_id == self.user.id:
            return
        emoji_str = str(payload.emoji)
        log.info(f"Reaction: {repr(emoji_str)} on msg {payload.message_id} by {payload.user_id}")
        if emoji_str not in _KEEP_VARIANTS and emoji_str not in _DISCARD_VARIANTS:
            return

        outcome = "kept" if emoji_str in _KEEP_VARIANTS else "discarded"
        try:
            link_id = _resolve_review(str(payload.message_id), outcome)
        except Exception as e:
            log.error(f"_resolve_review failed: {e}")
            return
        if not link_id:
            log.info(f"Message {payload.message_id} not in reviews or already resolved")
            return

        log.info(f"Link {link_id} → {outcome} by user {payload.user_id}")

    @tasks.loop(hours=1)
    async def daily_post(self):
        if datetime.now(_CENTRAL).hour != POST_HOUR_CENTRAL:
            return
        await self._post_review_link()

    @daily_post.before_loop
    async def before_daily(self):
        await self.wait_until_ready()

    async def _post_review_link(self, channel_id: int | None = None) -> bool:
        if not self.stream_id:
            return False
        cid = channel_id or THREAD_ID or CHANNEL_ID
        channel = self.get_channel(cid)
        if channel is None:
            # Threads are often not cached — fetch them directly.
            try:
                channel = await self.fetch_channel(cid)
            except Exception as e:
                log.error(f"Channel/thread {cid} not found: {e}")
                return False

        link = _pick_link(self.stream_id)
        if not link:
            await channel.send("📭 No links ready for review right now — check back later!")
            return False

        summary = await _ai_summary(link)
        embed = _build_embed(link, summary=summary)
        msg = await channel.send(
            content="📖 **YCPin — Time to review a link from Governance Studies**\n⬆️ upvote  ·  ⬇️ downvote (removes link)",
            embed=embed,
        )
        await msg.add_reaction(KEEP_EMOJI)
        await msg.add_reaction(DISCARD_EMOJI)

        _record_review(str(msg.id), link["id"], cid, self.stream_id)
        log.info(f"Posted link {link['id']} for review: {link['title'][:60]}")
        return True


async def _ai_summary(link: dict) -> str:
    if not ANTHROPIC_API_KEY:
        return link.get("summary", "")
    try:
        client = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        content_text = link.get("content_text") or ""
        has_url = (link.get("source") or "").startswith(("http://", "https://"))
        if content_text.strip():
            prompt = (
                f"Summarize this article in 2 concise sentences. "
                f"Title: {link['title']}\n\n{content_text[:3000]}"
            )
        elif not has_url:
            prompt = (
                f"In 2 sentences, describe what this paper or work is about: \"{link['title']}\""
            )
        else:
            return link.get("summary", "")
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": prompt}],
        )
        return msg.content[0].text.strip()
    except Exception as e:
        log.warning(f"AI summary failed: {e}")
        return link.get("summary", "")


def _build_embed(link: dict, summary: str = "") -> discord.Embed:
    source = link["source"] or ""
    url = source if source.startswith(("http://", "https://")) else None
    embed = discord.Embed(
        title=link["title"],
        url=url,
        color=EMBED_COLOR,
    )
    if summary:
        embed.description = summary

    footer_parts = []
    if link["posted_at"]:
        try:
            dt = datetime.fromisoformat(link["posted_at"].replace("Z", "+00:00"))
            footer_parts.append(f"Posted {dt.strftime('%b %d, %Y')}")
        except Exception:
            pass
    if link["tags"]:
        footer_parts.append("  ".join(f"#{t}" for t in link["tags"]))
    if link["keep_count"]:
        footer_parts.append(f"Reviewed {link['keep_count']}×")

    if footer_parts:
        embed.set_footer(text="  ·  ".join(footer_parts))

    return embed


# ── Slash commands ─────────────────────────────────────────────────────────

bot = YCPinBot()


@bot.tree.command(name="ycpin", description="Post a random link from Governance Studies for review")
@app_commands.describe(channel="Channel to post in (defaults to current channel)")
async def ycpin_command(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    target = channel or interaction.channel
    ok = await bot._post_review_link(channel_id=target.id)
    if ok:
        await interaction.followup.send(f"Posted a link for review in {target.mention}!", ephemeral=True)
    else:
        await interaction.followup.send("No reviewable links found.", ephemeral=True)


@bot.tree.command(name="ycstats", description="Show Pinboard review stats for Governance Studies")
async def ycstats_command(interaction: discord.Interaction):
    if not bot.stream_id:
        await interaction.response.send_message("Stream not found.", ephemeral=True)
        return
    s = _review_stats(bot.stream_id)
    embed = discord.Embed(title="📊 Governance Studies — Review Stats", color=EMBED_COLOR)
    embed.add_field(name="Total links", value=str(s["total"]), inline=True)
    embed.add_field(name="Unreviewed", value=str(s["unreviewed"]), inline=True)
    embed.add_field(name="⬆️ Upvoted", value=str(s["kept"]), inline=True)
    embed.add_field(name="⬇️ Downvoted", value=str(s["discarded"]), inline=True)
    await interaction.response.send_message(embed=embed)


# ── Main ──────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if not TOKEN:
        print("DISCORD_BOT_TOKEN is required.", file=sys.stderr)
        sys.exit(1)

    init_db(DB_PATH)
    target_desc = f"thread {THREAD_ID}" if THREAD_ID else f"channel {CHANNEL_ID}"
    log.info(f"Starting YCPin bot — posting to {target_desc} at {POST_HOUR_CENTRAL}:00 Central daily")
    bot.run(TOKEN, log_handler=None)
