#!/usr/bin/env python3
"""
Fetch links from a Discord channel and add them to a pinboard stream.

Usage:
    python scripts/fetch_discord.py --token BOT_TOKEN --channel CHANNEL_ID [--stream STREAM_NAME]

Or set env vars DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TaskProgressColumn, TimeRemainingColumn

# Add src to path so we can import pinboard
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from pinboard.config import DB_PATH, Config
from pinboard.db import get_conn, init_db
from pinboard.links import prepare_link, write_link
from pinboard.streams_ws import resolve_stream_id

console = Console()

# ── Social/junk domain filter ─────────────────────────────────────────────

BLOCKED_DOMAINS = {
    # social media
    "twitter.com", "x.com", "t.co",
    "bsky.app", "bsky.social",
    "instagram.com", "threads.net",
    "facebook.com", "fb.com",
    "tiktok.com",
    "reddit.com", "redd.it",
    "mastodon.social", "mastodon.online", "fosstodon.org",
    "warpcast.com", "farcaster.xyz",
    "discord.com", "discord.gg",
    "youtube.com", "youtu.be",
    "open.spotify.com",
    "chat.openai.com", "chatgpt.com",
    "linkedin.com",
    # not articles
    "docs.google.com",
    "roamresearch.com",
    "amazon.com", "amzn.to",
    "consensus.app",
    "tinyurl.com",
    "claude.ai",
    "readwise.io",
    "google.com",
    "mailchi.mp",
}

BLOCKED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mp3", ".pdf"}

URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def is_allowed(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower().removeprefix("www.")
        # block exact matches and subdomains
        for blocked in BLOCKED_DOMAINS:
            if host == blocked or host.endswith("." + blocked):
                return False
        # block media files
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in BLOCKED_EXTENSIONS):
            return False
        return True
    except Exception:
        return False


# ── Discord API ───────────────────────────────────────────────────────────

DISCORD_API = "https://discord.com/api/v10"


def fetch_all_messages(token: str, channel_id: str) -> list[dict]:
    """Page through all messages in the channel oldest-first."""
    headers = {"Authorization": f"Bot {token}"}
    messages = []
    before = None

    console.print(f"[dim]Fetching messages from channel {channel_id}…[/dim]")
    with httpx.Client(timeout=30) as client:
        while True:
            params = {"limit": 100}
            if before:
                params["before"] = before

            resp = client.get(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                headers=headers,
                params=params,
            )

            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1)
                console.print(f"[yellow]Rate limited — waiting {retry_after}s[/yellow]")
                time.sleep(float(retry_after))
                continue

            if resp.status_code == 403:
                console.print("[red]403 Forbidden — bot lacks Read Message History permission in this channel.[/red]")
                sys.exit(1)

            if not resp.is_success:
                console.print(f"[red]Discord API error {resp.status_code}: {resp.text}[/red]")
                sys.exit(1)

            batch = resp.json()
            if not batch:
                break

            messages.extend(batch)
            before = batch[-1]["id"]  # oldest in this batch

            console.print(f"[dim]  … {len(messages)} messages fetched[/dim]")
            time.sleep(0.5)  # be polite

    return messages


def extract_url_timestamps(messages: list[dict]) -> dict[str, str]:
    """Return {url: earliest_message_timestamp} for all allowed URLs."""
    url_ts: dict[str, str] = {}
    for msg in messages:
        ts = msg.get("timestamp", "")
        text = msg.get("content", "")
        for embed in msg.get("embeds", []):
            if embed.get("url"):
                text += " " + embed["url"]
        for url in URL_RE.findall(text):
            url = url.rstrip(".,;:!?)>")
            if is_allowed(url):
                # keep the earliest timestamp a URL was posted
                if url not in url_ts or ts < url_ts[url]:
                    url_ts[url] = ts
    return url_ts


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Import Discord channel links into pinboard.")
    parser.add_argument("--token", default=os.environ.get("DISCORD_BOT_TOKEN"), help="Discord bot token")
    parser.add_argument("--channel", default=os.environ.get("DISCORD_CHANNEL_ID"), help="Discord channel ID")
    parser.add_argument("--stream", default="Governance Studies", help="Pinboard stream name (default: Governance Studies)")
    parser.add_argument("--dry-run", action="store_true", help="Print URLs without adding them")
    args = parser.parse_args()

    if not args.token:
        console.print("[red]--token or DISCORD_BOT_TOKEN required[/red]")
        sys.exit(1)
    if not args.channel:
        console.print("[red]--channel or DISCORD_CHANNEL_ID required[/red]")
        sys.exit(1)

    init_db(DB_PATH)

    with get_conn(DB_PATH) as db:
        try:
            stream_id = resolve_stream_id(db, args.stream)
        except ValueError:
            console.print(f"[red]Stream '{args.stream}' not found. Available streams:[/red]")
            for row in db.execute("SELECT name FROM streams").fetchall():
                console.print(f"  • {row['name']}")
            sys.exit(1)

        # Collect existing sources to avoid duplicates
        existing = {
            r["source"]
            for r in db.execute("SELECT source FROM links WHERE stream_id = ?", (stream_id,)).fetchall()
            if r["source"]
        }

    console.print(f"[bold]Stream:[/bold] {args.stream} ({stream_id})")
    console.print(f"[bold]Existing links:[/bold] {len(existing)}")

    messages = fetch_all_messages(args.token, args.channel)
    console.print(f"[bold]Total messages:[/bold] {len(messages)}")

    url_ts = extract_url_timestamps(messages)
    new_urls = [u for u in url_ts if u not in existing]

    console.print(f"[bold]URLs found:[/bold] {len(url_ts)}  [bold]New:[/bold] {len(new_urls)}")

    if args.dry_run:
        console.print("\n[bold]Dry run — URLs that would be added:[/bold]")
        for url in new_urls:
            console.print(f"  {url_ts[url][:10]}  {url}")
        return

    with get_conn(DB_PATH) as db:
        stream_row = db.execute("SELECT name FROM streams WHERE id = ?", (stream_id,)).fetchone()
        stream_name = stream_row["name"]

    # Backfill posted_at for links already in the DB that have no timestamp yet
    backfill = [(url_ts[u], u) for u in url_ts if u in existing]
    if backfill:
        console.print(f"[dim]Backfilling posted_at for {len(backfill)} existing links…[/dim]")
        with get_conn(DB_PATH) as db:
            db.executemany(
                "UPDATE links SET posted_at = ? WHERE source = ? AND stream_id = ? AND posted_at IS NULL",
                [(ts, url, stream_id) for ts, url in backfill],
            )

    if not new_urls:
        console.print("[green]Nothing new to add.[/green]")
        return

    added = 0
    failed = 0
    db_lock = threading.Lock()

    def ingest(url: str) -> tuple[bool, str]:
        try:
            prepared = prepare_link(
                url, stream_name=stream_name, stream_id=stream_id,
                posted_at=url_ts.get(url),
            )
            with db_lock:
                with get_conn(DB_PATH) as db:
                    write_link(db, prepared)
            return True, url
        except Exception as e:
            return False, f"{url[:70]} — {e}"

    with Progress(
        SpinnerColumn(),
        "[progress.description]{task.description}",
        BarColumn(),
        TaskProgressColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Ingesting links…", total=len(new_urls))
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(ingest, url): url for url in new_urls}
            for future in as_completed(futures):
                ok, msg = future.result()
                if ok:
                    added += 1
                else:
                    failed += 1
                    console.print(f"[yellow]  ✗ {msg}[/yellow]")
                progress.advance(task)

    console.print(f"\n[green]Done.[/green] Added {added}, failed {failed}.")


if __name__ == "__main__":
    main()
