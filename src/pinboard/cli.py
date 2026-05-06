"""Pinboard CLI — all commands."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .streams_ws import (
    create_stream, list_streams, resolve_stream_id,
    get_active_stream_id, set_active_stream_id,
)
from .config import Config, DB_PATH, PINBOARD_DIR, ARTIFACTS_DIR
from .db import init_db, get_conn
from . import connections as conn_mod
from . import pins as pin_mod
from . import links as link_mod
from .events import record, now_utc
from .output import emit, print_error, print_success, print_info
from .scoring import lab_scores, link_score, pin_relevance_score
from . import skills as skills_mod

app = typer.Typer(help="Pinboard: local-first personal pinning & links system.", no_args_is_help=True)
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ensure_init():
    if not DB_PATH.exists():
        print_error("Pinboard not initialized. Run: pinboard init")
        raise typer.Exit(1)


def _active_stream(db, override: str | None = None) -> tuple[str, str]:
    """Return (stream_id, stream_name) for the active or overridden stream."""
    if override:
        sid = resolve_stream_id(db, override)
    else:
        sid = get_active_stream_id()
        row = db.execute("SELECT id, name FROM streams WHERE id = ?", (sid,)).fetchone()
        if not row:
            row = db.execute("SELECT id, name FROM streams ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                print_error("No streams found. Run: pinboard init")
                raise typer.Exit(1)
            sid = row["id"]
    row = db.execute("SELECT name FROM streams WHERE id = ?", (sid,)).fetchone()
    return sid, row["name"]


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

@app.command()
def init():
    """Initialize the pinboard database and directory structure."""
    PINBOARD_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    init_db(DB_PATH)
    print_success(f"Pinboard initialized at {PINBOARD_DIR}")
    print_info("Default stream 'default' is active.")


# ---------------------------------------------------------------------------
# stream commands
# ---------------------------------------------------------------------------

stream_app = typer.Typer(help="Manage streams (workspaces).")
app.add_typer(stream_app, name="stream")


@stream_app.callback(invoke_without_command=True)
def stream_default(ctx: typer.Context):
    if not ctx.invoked_subcommand:
        _ensure_init()
        with get_conn(DB_PATH) as db:
            rows = list_streams(db)
        for r in rows:
            marker = "▶" if r["active"] else " "
            console.print(f"  {marker} [bold]{r['name']}[/bold]  [dim]{r['pin_count']} pins  {r['link_count']} links[/dim]")


@stream_app.command("create")
def stream_create(
    name: str = typer.Argument(..., help="Stream name"),
    switch: bool = typer.Option(True, "--switch/--no-switch", help="Switch to new stream after creating"),
):
    """Create a new stream."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            sid = create_stream(db, name)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    if switch:
        set_active_stream_id(sid)
        print_success(f"Created and switched to stream '{name}'")
    else:
        print_success(f"Created stream '{name}'  id={sid}")


@stream_app.command("switch")
def stream_switch(name_or_id: str = typer.Argument(..., help="Stream name or id")):
    """Switch the active stream."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            sid = resolve_stream_id(db, name_or_id)
            row = db.execute("SELECT name FROM streams WHERE id = ?", (sid,)).fetchone()
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    set_active_stream_id(sid)
    print_success(f"Switched to stream '{row['name']}'")


@stream_app.command("ls")
def stream_ls():
    """List all streams."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        rows = list_streams(db)
    for r in rows:
        marker = "▶" if r["active"] else " "
        console.print(f"  {marker} [bold]{r['name']}[/bold]  [dim]{r['pin_count']} pins  {r['link_count']} links  id={r['id']}[/dim]")


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@app.command()
def add(
    source: str = typer.Argument(..., help="URL, file path, or note text"),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    note: Optional[str] = typer.Option(None, "--note", "-n"),
    cache: bool = typer.Option(False, "--cache"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s", help="Stream name (default: active)"),
):
    """Add a link to the active stream."""
    _ensure_init()
    cfg = Config.load()
    from .embeddings import build_service
    embedder = build_service(cfg)

    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        lid = link_mod.add_link(
            db, source, stream_id=stream_id, title=title, note=note, cache=cache, embedder=embedder
        )
        lnk = db.execute("SELECT * FROM links WHERE id = ?", (lid,)).fetchone()
        suggested = conn_mod.auto_suggest(db, lid, cfg, stream_id)

    print_success(f"[{stream_name}] Added link [{lnk['kind']}] {lnk['title']!r}  id={lid}")
    if suggested:
        print_info(f"  → {len(suggested)} connection suggestion(s). Run: pinboard connections --pending")


# ---------------------------------------------------------------------------
# add-batch
# ---------------------------------------------------------------------------

@app.command("add-batch")
def add_batch(
    sources: list[str] = typer.Argument(None, help="URLs or file paths to add"),
    stream: str = typer.Option(..., "--stream", "-s", help="Stream name (required)"),
    create: bool = typer.Option(False, "--create", help="Create stream if it doesn't exist"),
    from_file: Optional[Path] = typer.Option(None, "--from-file", "-f", help="Text file with one source per line"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Note applied to every link"),
    workers: int = typer.Option(8, "--workers", "-w", help="Parallel worker threads (default: 8)"),
):
    """Add multiple links to a stream in one go.

    Examples:
      pinboard add-batch --stream "Governance Studies" file1.pdf https://...
      pinboard add-batch --stream "New Topic" --create --from-file sources.txt
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

    _ensure_init()
    cfg = Config.load()
    from .embeddings import build_service
    embedder = build_service(cfg)

    with get_conn(DB_PATH) as db:
        try:
            stream_id, stream_name = _active_stream(db, stream)
        except (typer.Exit, ValueError):
            if not create:
                print_error(f"Stream '{stream}' not found. Use --create to create it.")
                raise typer.Exit(1)
            stream_id = create_stream(db, stream)
            stream_name = stream
            print_success(f"Created stream '{stream_name}'")

    all_sources: list[str] = list(sources or [])
    if from_file:
        if not from_file.exists():
            print_error(f"File not found: {from_file}")
            raise typer.Exit(1)
        lines = [l.strip() for l in from_file.read_text().splitlines() if l.strip() and not l.startswith("#")]
        all_sources.extend(lines)

    if not all_sources:
        print_error("No sources provided. Pass paths/URLs as arguments or use --from-file.")
        raise typer.Exit(1)

    console.print(f"\n[bold cyan]Adding {len(all_sources)} link(s) to [{stream_name}][/bold cyan]  [dim]workers={workers}[/dim]\n")

    added: list[tuple] = []
    failed: list[tuple] = []
    total_connections = 0
    db_lock = threading.Lock()
    results_lock = threading.Lock()

    def _ingest_one(source: str) -> tuple:
        try:
            prepared = link_mod.prepare_link(
                source, stream_name=stream_name, stream_id=stream_id,
                note=note, embedder=embedder,
            )
            with db_lock:
                with get_conn(DB_PATH) as db:
                    lid = link_mod.write_link(db, prepared)
            with get_conn(DB_PATH) as db:
                pending_conns = conn_mod.prepare_auto_connections(db, lid, cfg, stream_id)
            if pending_conns:
                with db_lock:
                    with get_conn(DB_PATH) as db:
                        conn_mod.write_auto_connections(db, pending_conns)
            return (True, source, lid, prepared["title"], prepared["kind"], len(pending_conns))
        except Exception as e:
            return (False, source, str(e), None, None, 0)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Ingesting…", total=len(all_sources))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_ingest_one, src): src for src in all_sources}
            for future in as_completed(futures):
                result = future.result()
                ok, source, *rest = result
                with results_lock:
                    if ok:
                        lid, title, kind, n_conn = rest
                        added.append((source, lid, title, kind))
                        total_connections += n_conn
                    else:
                        err = rest[0]
                        failed.append((source, err))
                progress.advance(task)

    console.print(f"\n[bold green]✓ {len(added)} added[/bold green]", end="")
    if failed:
        console.print(f"  [bold red]✗ {len(failed)} failed[/bold red]", end="")
    if total_connections:
        console.print(f"  [dim]{total_connections} connection suggestion(s)[/dim]", end="")
    console.print()

    for source, lid, title, kind in added:
        console.print(f"  [green]✓[/green] [{kind}] {title!r}  [dim]{lid}[/dim]")
    for source, err in failed:
        console.print(f"  [red]✗[/red] {source}  [dim]{err}[/dim]")
    if total_connections:
        print_info(f"\nRun: pinboard connections --pending --stream {stream_name!r}")


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

@app.command("ls")
def list_links(
    pins_only: bool = typer.Option(False, "--pins-only"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """List pin clusters then recent links in the active stream."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        pins = pin_mod.active_pins(db, stream_id)
        rows = []

        for p in pins:
            for pl in p["links"]:
                rows.append({
                    "id": pl["id"], "kind": pl["kind"], "title": pl["title"],
                    "source": pl["source"], "artifact_path": pl["artifact_path"],
                    "created_at": pl["added_at"], "pinned": "★",
                    "pin_name": p["name"], "channel": stream_name,
                })

        if not pins_only:
            recent = db.execute(
                """
                SELECT l.id, l.kind, l.title, l.source, l.artifact_path, l.created_at
                FROM links l
                WHERE l.stream_id = ?
                  AND l.id NOT IN (
                      SELECT pl.link_id FROM pin_links pl
                      JOIN pins p ON p.id = pl.pin_id
                      WHERE p.stream_id = ? AND p.closed_at IS NULL
                  )
                ORDER BY l.created_at DESC LIMIT 50
                """,
                (stream_id, stream_id),
            ).fetchall()
            rows += [
                dict(r) | {"pinned": "", "pin_name": "", "channel": stream_name,
                            "pin_score": pin_relevance_score(db, r["id"], stream_id)}
                for r in recent
            ]

    if not link and not as_json and not pins_only:
        console.print(f"\n[bold cyan]Stream: {stream_name}[/bold cyan]  "
                      f"[dim]({pin_mod.MAX_PINS_PER_STREAM} pin clusters max, {pin_mod.MAX_LINKS_PER_PIN} links per cluster)[/dim]")

    emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# pin commands
# ---------------------------------------------------------------------------

pin_app = typer.Typer(help="Manage pin clusters.")
app.add_typer(pin_app, name="pin")


@pin_app.callback(invoke_without_command=True)
def pin_default(ctx: typer.Context):
    if not ctx.invoked_subcommand:
        _ensure_init()
        with get_conn(DB_PATH) as db:
            stream_id, stream_name = _active_stream(db)
            pins = pin_mod.active_pins(db, stream_id)
        console.print(f"\n[bold cyan]Pin clusters — {stream_name}[/bold cyan]\n")
        for p in pins:
            console.print(f"  [bold magenta]★ {p['name']}[/bold magenta]  [dim]slot {p['slot_order']}  id={p['id']}[/dim]")
            if p.get("note"):
                console.print(f"    [italic]{p['note']}[/italic]")
            for pl in p["links"]:
                console.print(f"    • {pl['title']}  [dim]{pl['id']}[/dim]")
        if not pins:
            print_info("No active pin clusters. Run: pinboard pin create \"Cluster Name\"")


@pin_app.command("create")
def pin_create(
    name: str = typer.Argument(..., help="Cluster name"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Why this cluster, why now"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
):
    """Create a new pin cluster."""
    _ensure_init()
    if not note:
        note = typer.prompt("Pin note (why this cluster, why now?)", default="", show_default=False) or None
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        try:
            pin_id = pin_mod.create_pin(db, stream_id, name, note=note)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"[{stream_name}] Created pin cluster '{name}'  id={pin_id}")


@pin_app.command("add")
def pin_add(
    pin_id: str = typer.Argument(..., help="Pin cluster id or slot number"),
    link_id: str = typer.Argument(..., help="Link id to add to the cluster"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
):
    """Add a link to a pin cluster."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        resolved_pin = pin_mod.resolve_pin_id(db, stream_id, pin_id)
        if not resolved_pin:
            print_error(f"Pin cluster not found: {pin_id}")
            raise typer.Exit(1)
        try:
            pl_id = pin_mod.add_link_to_pin(db, resolved_pin, link_id)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
        pin_row = db.execute("SELECT name FROM pins WHERE id = ?", (resolved_pin,)).fetchone()
        link_row = db.execute("SELECT title FROM links WHERE id = ?", (link_id,)).fetchone()

    print_success(f"[{stream_name}] Added '{link_row['title']}' to cluster '{pin_row['name']}'")

    # Generate skill from aggregated cluster content
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        cluster_links = db.execute(
            """
            SELECT l.title, l.content_text FROM pin_links pl
            JOIN links l ON l.id = pl.link_id WHERE pl.pin_id = ?
            """,
            (resolved_pin,),
        ).fetchall()
    combined_text = "\n\n".join((r["content_text"] or "") for r in cluster_links if r["content_text"])
    combined_title = " + ".join(r["title"] for r in cluster_links)
    if combined_text:
        print_info("Generating pin skill…")
        skill = skills_mod.generate_skill(cfg, resolved_pin, combined_title, combined_text)
        if skill:
            with get_conn(DB_PATH) as db:
                skills_mod.save_skill(db, resolved_pin, skill)
            print_success(f"Skill updated: {len(skill.get('themes', []))} themes")


@pin_app.command("remove")
def pin_remove(
    pin_id: str = typer.Argument(..., help="Pin cluster id or slot number"),
    link_id: str = typer.Argument(..., help="Link id to remove"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
):
    """Remove a link from a pin cluster."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        resolved_pin = pin_mod.resolve_pin_id(db, stream_id, pin_id)
        if not resolved_pin:
            print_error(f"Pin cluster not found: {pin_id}")
            raise typer.Exit(1)
        try:
            pin_mod.remove_link_from_pin(db, resolved_pin, link_id)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"[{stream_name}] Removed link from cluster.")


@pin_app.command("close")
def pin_close(
    id_or_slot: str = typer.Argument(..., help="Pin cluster id or slot number (1-3)"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
):
    """Close a pin cluster."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        try:
            pin_id = pin_mod.close_pin(db, stream_id, id_or_slot)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"[{stream_name}] Closed pin cluster  pin_id={pin_id}")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------

@app.command("open")
def open_link(link_id: str = typer.Argument(...)):
    """Open a link in the browser; records an open event."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        row = db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            print_error(f"Link {link_id} not found.")
            raise typer.Exit(1)
        record(db, "open", stream_id=link_id)
        target = row["source"] if row["kind"] == "url" else (row["artifact_path"] or row["source"])

    if sys.platform == "darwin":
        subprocess.run(["open", target])
    elif sys.platform.startswith("linux"):
        subprocess.run(["xdg-open", target])
    else:
        subprocess.run(["start", target], shell=True)
    print_info(f"Opened: {target}")


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------

@app.command()
def connections(
    pending: bool = typer.Option(False, "--pending"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """List connections between links and pin clusters."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        query = """
            SELECT c.id, c.link_id, c.pin_id, c.similarity, c.llm_note,
                   c.confirmed, c.source, c.created_at,
                   l.title as link_title, l.source as link_source, l.artifact_path,
                   p.name as pin_name
            FROM connections c
            JOIN links l ON l.id = c.link_id
            JOIN pins p ON p.id = c.pin_id
            WHERE p.stream_id = ?
        """
        params = [stream_id]
        if pending:
            query += " AND c.confirmed = FALSE"
        query += " ORDER BY c.similarity DESC"
        rows = [dict(r) for r in db.execute(query, params).fetchall()]

    if as_json or link:
        emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)
        return
    if not rows:
        print_info(f"No connections in stream '{stream_name}'.")
        return
    if n:
        rows = rows[:n]
    for row in rows:
        status = "[green]✓ confirmed[/green]" if row["confirmed"] else "[yellow]? pending[/yellow]"
        console.print(f"\n[bold]{row['link_title']}[/bold]  →  [magenta]{row['pin_name']}[/magenta]  {status}")
        console.print(f"  [dim]id: {row['id']}  sim: {row['similarity']:.2f}  source: {row['source']}[/dim]")
        if row["llm_note"]:
            console.print(f"  [italic]{row['llm_note']}[/italic]")


# ---------------------------------------------------------------------------
# confirm / reject / link
# ---------------------------------------------------------------------------

@app.command()
def confirm(conn_id: str = typer.Argument(...)):
    """Confirm a suggested connection."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            conn_mod.confirm_connection(db, conn_id)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Connection {conn_id} confirmed.")


@app.command()
def reject(conn_id: str = typer.Argument(...)):
    """Reject and delete a suggested connection."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            conn_mod.reject_connection(db, conn_id)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Connection {conn_id} rejected.")


@app.command()
def link(
    link_id: str = typer.Argument(...),
    pin_id: str = typer.Argument(...),
    note: Optional[str] = typer.Option(None, "--note", "-n"),
):
    """Manually link a link to a pin cluster (always confirmed)."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            cid = conn_mod.manual_link(db, link_id, pin_id, note=note)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Linked  connection_id={cid}")


# ---------------------------------------------------------------------------
# lab
# ---------------------------------------------------------------------------

@app.command()
def lab(
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "--select"),
    n: int = typer.Option(20, "-n"),
):
    """Show unpinned links gaining traction."""
    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        rows = lab_scores(db, stream_id=stream_id, half_life_days=cfg.half_life_days, limit=n)
    emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------

@app.command()
def why(link_id: str = typer.Argument(...)):
    """Show the event timeline for a link."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        lnk = db.execute("SELECT title FROM links WHERE id = ?", (link_id,)).fetchone()
        if not lnk:
            print_error(f"Link {link_id} not found.")
            raise typer.Exit(1)
        events = db.execute(
            "SELECT event_type, occurred_at, metadata_json FROM events WHERE stream_id = ? ORDER BY occurred_at",
            (link_id,),
        ).fetchall()

    console.print(f"\n[bold]Timeline for:[/bold] {lnk['title']}")
    for ev in events:
        meta = json.loads(ev["metadata_json"]) if ev["metadata_json"] else {}
        console.print(f"  [cyan]{ev['occurred_at']}[/cyan]  [bold]{ev['event_type']}[/bold]  {meta}")
    if not events:
        print_info("No events recorded for this link yet.")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

edit_app = typer.Typer(help="Edit link or pin fields in $EDITOR.")
app.add_typer(edit_app, name="edit")


@edit_app.callback(invoke_without_command=True)
def edit_link(
    ctx: typer.Context,
    link_id: Optional[str] = typer.Argument(None),
):
    """Edit a link's editable fields (title, note)."""
    if ctx.invoked_subcommand:
        return
    if not link_id:
        print_error("Provide a link ID or use: pinboard edit pin <id>")
        raise typer.Exit(1)

    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        row = db.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            print_error(f"Link {link_id} not found.")
            raise typer.Exit(1)

        import yaml
        buffer = {"title": row["title"], "note": row["note"] or ""}
        original = yaml.dump(buffer, allow_unicode=True)

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tf:
            tf.write(original)
            tmp_path = tf.name

        subprocess.run([cfg.effective_editor(), tmp_path])
        edited = Path(tmp_path).read_text()
        Path(tmp_path).unlink(missing_ok=True)

        try:
            updated = yaml.safe_load(edited)
        except Exception as e:
            print_error(f"Invalid YAML: {e}")
            raise typer.Exit(1)

        if updated == buffer:
            print_info("No changes.")
            return

        db.execute(
            "UPDATE links SET title = ?, note = ? WHERE id = ?",
            (updated.get("title", row["title"]), updated.get("note") or None, link_id),
        )
        record(db, "edit", stream_id=link_id, metadata={"fields": list(updated.keys())})

    print_success(f"Link {link_id} updated.")


@edit_app.command("pin")
def edit_pin(id_or_slot: str = typer.Argument(...)):
    """Edit a pin cluster's name and note in $EDITOR."""
    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        stream_id, _ = _active_stream(db)
        pin_id = pin_mod.resolve_pin_id(db, stream_id, id_or_slot)
        if not pin_id:
            print_error(f"No active pin cluster found for: {id_or_slot}")
            raise typer.Exit(1)

        row = db.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
        import yaml
        buffer = {"name": row["name"], "note": row["note"] or ""}
        original = yaml.dump(buffer, allow_unicode=True)

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tf:
            tf.write(original)
            tmp_path = tf.name

        subprocess.run([cfg.effective_editor(), tmp_path])
        edited = Path(tmp_path).read_text()
        Path(tmp_path).unlink(missing_ok=True)

        try:
            updated = yaml.safe_load(edited)
        except Exception as e:
            print_error(f"Invalid YAML: {e}")
            raise typer.Exit(1)

        if updated == buffer:
            print_info("No changes.")
            return

        db.execute(
            "UPDATE pins SET name = ?, note = ? WHERE id = ?",
            (updated.get("name", row["name"]), updated.get("note") or None, pin_id),
        )
        record(db, "edit", pin_id=pin_id, metadata={"fields": list(updated.keys())})

    print_success(f"Pin cluster {pin_id} updated.")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(...),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "--select"),
    n: int = typer.Option(10, "-n"),
):
    """Semantic search over links in the active stream."""
    _ensure_init()
    cfg = Config.load()
    from .embeddings import build_service, deserialize, cosine_similarity
    embedder = build_service(cfg)
    if not embedder:
        print_error("No embedding service configured.")
        raise typer.Exit(1)

    query_vec = embedder.embed(query)

    with get_conn(DB_PATH) as db:
        stream_id, _ = _active_stream(db, stream)
        rows = db.execute(
            "SELECT id, title, kind, source, artifact_path, embedding FROM links WHERE stream_id = ? AND embedding IS NOT NULL",
            (stream_id,),
        ).fetchall()

    results = []
    for row in rows:
        vec = deserialize(row["embedding"])
        sim = cosine_similarity(query_vec, vec)
        results.append({
            "id": row["id"], "title": row["title"], "kind": row["kind"],
            "source": row["source"], "artifact_path": row["artifact_path"],
            "similarity": round(sim, 4),
        })
    results.sort(key=lambda r: r["similarity"], reverse=True)
    emit(results[:n], link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

@app.command()
def graph(
    pin_id: Optional[str] = typer.Option(None, "--pin"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """Show the confirmed connection graph."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        q = """
            SELECT c.id, c.similarity, c.confirmed, c.source, c.llm_note,
                   l.title as link_title, l.kind as link_kind, l.source as link_source,
                   l.artifact_path, p.name as pin_name, p.id as pin_id
            FROM connections c
            JOIN links l ON l.id = c.link_id
            JOIN pins p ON p.id = c.pin_id
            WHERE c.confirmed = TRUE AND p.stream_id = ?
        """
        params = [stream_id]
        if pin_id:
            q += " AND c.pin_id = ?"
            params.append(pin_id)
        q += " ORDER BY c.similarity DESC"
        rows = [dict(r) for r in db.execute(q, params).fetchall()]

    if as_json:
        out = [_pick_select(r, select) for r in rows]
        print(json.dumps(out, indent=2 if pretty else None, default=str))
        return
    if not rows:
        print_info(f"No confirmed connections in stream '{stream_name}'.")
        return

    by_pin: dict[str, list] = {}
    for r in rows:
        by_pin.setdefault(r["pin_name"], []).append(r)

    console.print(f"\n[bold cyan]Stream: {stream_name}[/bold cyan]")
    for pt, edges in by_pin.items():
        console.print(f"\n[bold magenta]★ {pt}[/bold magenta]")
        for e in edges:
            console.print(f"  ✓ {e['link_title']} [dim](sim={e['similarity']:.2f})[/dim]")
            if e["llm_note"]:
                console.print(f"    [italic]{e['llm_note']}[/italic]")


def _pick_select(row: dict, select: str | None) -> dict:
    if not select:
        return row
    fields = [f.strip() for f in select.split(",")]
    return {k: row[k] for k in fields if k in row}


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

@app.command("export")
def export_data(
    fmt: str = typer.Option("json", "--format", "-f"),
    stream: Optional[str] = typer.Option(None, "--stream", "-s", help="Export specific stream (default: all)"),
):
    """Export all data for backup."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        streams_rows = [dict(r) for r in db.execute("SELECT * FROM streams").fetchall()]

        if stream:
            stream_id, _ = _active_stream(db, stream)
            links_rows = [dict(r) for r in db.execute("SELECT * FROM links WHERE stream_id = ?", (stream_id,)).fetchall()]
            pins_rows = [dict(r) for r in db.execute("SELECT * FROM pins WHERE stream_id = ?", (stream_id,)).fetchall()]
        else:
            links_rows = [dict(r) for r in db.execute("SELECT * FROM links").fetchall()]
            pins_rows = [dict(r) for r in db.execute("SELECT * FROM pins").fetchall()]

        pin_links_rows = [dict(r) for r in db.execute("SELECT * FROM pin_links").fetchall()]
        conn_rows = [dict(r) for r in db.execute("SELECT * FROM connections").fetchall()]
        events_rows = [dict(r) for r in db.execute("SELECT * FROM events").fetchall()]

        for l in links_rows:
            l.pop("embedding", None)

    out = {
        "streams": streams_rows, "links": links_rows, "pins": pins_rows,
        "pin_links": pin_links_rows, "connections": conn_rows, "events": events_rows,
    }
    print(json.dumps(out, indent=2, default=str))


# ---------------------------------------------------------------------------
# skills
# ---------------------------------------------------------------------------

@app.command()
def skills(
    stream: Optional[str] = typer.Option(None, "--stream", "-s"),
):
    """Show the decomposed skills for all active pin clusters."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream_id, stream_name = _active_stream(db, stream)
        all_skills = skills_mod.get_skills_for_stream(db, stream_id)

    if not all_skills:
        print_info(f"No skills yet in '{stream_name}'. Add links to a pin cluster to generate a skill.")
        return

    console.print(f"\n[bold cyan]Pin Skills — #{stream_name}[/bold cyan]\n")
    for s in all_skills:
        console.print(f"[bold magenta]★ {s['pin_title']}[/bold magenta]")
        console.print(f"  [bold]Themes:[/bold] {', '.join(s['themes'])}")
        console.print(f"  [bold]Questions:[/bold]")
        for q in s["questions"]:
            console.print(f"    · {q}")
        console.print(f"  [bold]Adjacent:[/bold] {', '.join(s['adjacent'])}")
        console.print(f"  [bold]Search signals:[/bold]")
        for sig in s["search_signals"]:
            console.print(f"    → {sig}")
        console.print()


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------

@app.command()
def digest(
    dry_run: bool = typer.Option(False, "--dry-run", help="Print digest without sending"),
):
    """Generate and send the daily Claude-curated reading digest via Telegram."""
    _ensure_init()
    cfg = Config.load()
    from .digest import build_digest, render_plain, send_telegram
    from datetime import datetime, timezone

    print_info("Building digest…")
    with get_conn(DB_PATH) as db:
        data = build_digest(db, cfg)

    if not data:
        print_info("No links to digest yet. Add some links first.")
        return

    date_str = datetime.now(timezone.utc).strftime("%A, %B %-d %Y")
    plain = render_plain(data, date_str)

    if dry_run:
        console.print(plain)
        return

    if not cfg.extra.get("telegram_bot_token") or not cfg.extra.get("telegram_chat_id"):
        print_error("Telegram not configured. Add telegram_bot_token and telegram_chat_id to ~/.pinboard/config.toml")
        raise typer.Exit(1)

    ok = send_telegram(cfg, data, date_str)
    if ok:
        print_success(f"Digest sent to Telegram ({len(data)} stream(s))")
    else:
        print_error("Failed to send Telegram message.")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------

@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(5000, "--port"),
):
    """Start the Pinboard web UI server."""
    try:
        import uvicorn
    except ImportError:
        print_error("uvicorn not installed. Run: pip install 'pinboard[web]'")
        raise typer.Exit(1)
    _ensure_init()
    print_success(f"Pinboard UI at http://{host}:{port}")
    uvicorn.run("pinboard.api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
