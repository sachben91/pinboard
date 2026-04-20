"""Pinboard CLI — all commands."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from .config import Config, DB_PATH, PINBOARD_DIR, ARTIFACTS_DIR
from .db import init_db, get_conn
from . import connections as conn_mod
from . import pins as pin_mod
from . import streams as stream_mod
from .events import record, now_utc
from .output import emit, print_error, print_success, print_info
from .scoring import lab_scores, stream_score

app = typer.Typer(help="Pinboard: local-first personal pinning & streams system.", no_args_is_help=True)
console = Console()


# ---------------------------------------------------------------------------
# Output flag helpers
# ---------------------------------------------------------------------------

def _out_opts(
    link: bool = typer.Option(False, "-l", "--link", help="Output only URLs/paths, one per line"),
    as_json: bool = typer.Option(False, "-j", "--json", help="Output JSON"),
    pretty: bool = typer.Option(False, "-P", "--pretty", help="Pretty-print JSON"),
    select: Optional[str] = typer.Option(None, "-s", "--select", help="Comma-separated fields"),
    n: Optional[int] = typer.Option(None, "-n", help="Limit results"),
):
    return dict(link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


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


def _ensure_init():
    if not DB_PATH.exists():
        print_error("Pinboard not initialized. Run: pinboard init")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------

@app.command()
def add(
    source: str = typer.Argument(..., help="URL, file path, or note text"),
    title: Optional[str] = typer.Option(None, "--title", "-t"),
    note: Optional[str] = typer.Option(None, "--note", "-n"),
    cache: bool = typer.Option(False, "--cache", help="Cache HTML snapshot for URLs"),
):
    """Add a stream (URL, PDF, image, note, or doc)."""
    _ensure_init()
    cfg = Config.load()
    from .embeddings import build_service
    embedder = build_service(cfg)

    with get_conn(DB_PATH) as db:
        stream_id = stream_mod.add_stream(db, source, title=title, note=note, cache=cache, embedder=embedder)
        stream = db.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()

        # Auto-suggest connections against active pins
        suggested = conn_mod.auto_suggest(db, stream_id, cfg)

    print_success(f"Added stream [{stream['kind']}] {stream['title']!r}  id={stream_id}")
    if suggested:
        print_info(f"  → {len(suggested)} connection suggestion(s) generated. Run: pinboard connections --pending")


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------

@app.command("ls")
def list_streams(
    pins_only: bool = typer.Option(False, "--pins-only", help="Show only pinned streams"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "-s", "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """List active pins (top) then recent streams."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        active = db.execute(
            """
            SELECT p.slot_order as slot, s.id, s.kind, s.title, s.source,
                   s.artifact_path, s.created_at, p.note as pin_note, p.id as pin_id
            FROM pins p JOIN streams s ON s.id = p.stream_id
            WHERE p.unpinned_at IS NULL
            ORDER BY p.slot_order
            """
        ).fetchall()

        rows = [dict(r) | {"_pinned": True} for r in active]

        if not pins_only:
            recent = db.execute(
                """
                SELECT s.id, s.kind, s.title, s.source, s.artifact_path, s.created_at
                FROM streams s
                WHERE s.id NOT IN (SELECT stream_id FROM pins WHERE unpinned_at IS NULL)
                ORDER BY s.created_at DESC
                LIMIT 50
                """
            ).fetchall()
            rows += [dict(r) | {"_pinned": False, "slot": ""} for r in recent]

    if not link and not as_json and rows:
        # Annotate pinned rows for display
        for r in rows:
            r["pinned"] = "★" if r.get("_pinned") else ""
        for r in rows:
            r.pop("_pinned", None)

    emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# pin
# ---------------------------------------------------------------------------

@app.command()
def pin(
    stream_id: str = typer.Argument(..., help="Stream ID to pin"),
    note: Optional[str] = typer.Option(None, "--note", "-n", help="Why you're pinning this"),
):
    """Pin a stream (max 5 active pins)."""
    _ensure_init()
    if not note:
        note = typer.prompt("Pin note (why this, why now?)", default="", show_default=False) or None

    with get_conn(DB_PATH) as db:
        try:
            pin_id = pin_mod.pin_stream(db, stream_id, note=note)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)

    print_success(f"Pinned stream {stream_id}  pin_id={pin_id}")


# ---------------------------------------------------------------------------
# unpin
# ---------------------------------------------------------------------------

@app.command()
def unpin(
    id_or_slot: str = typer.Argument(..., help="Pin id, stream id, or slot number (1-5)"),
):
    """Unpin a stream. Slots are re-numbered automatically."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            pin_id = pin_mod.unpin(db, id_or_slot)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Unpinned  pin_id={pin_id}")


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------

@app.command("open")
def open_stream(
    stream_id: str = typer.Argument(..., help="Stream ID to open"),
):
    """Open a stream in the browser or default app; records an open event."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        row = db.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
        if not row:
            print_error(f"Stream {stream_id} not found.")
            raise typer.Exit(1)
        record(db, "open", stream_id=stream_id)
        target = row["source"] if row["kind"] == "url" else (row["artifact_path"] or row["source"])

    import subprocess
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
    pending: bool = typer.Option(False, "--pending", help="Show only unconfirmed suggestions"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "-s", "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """List connections between streams and pins."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        query = """
            SELECT c.id, c.stream_id, c.pin_id, c.similarity, c.llm_note,
                   c.confirmed, c.source, c.created_at,
                   s.title as stream_title, s.source as stream_source,
                   s.artifact_path,
                   ps.title as pin_title
            FROM connections c
            JOIN streams s ON s.id = c.stream_id
            JOIN pins p ON p.id = c.pin_id
            JOIN streams ps ON ps.id = p.stream_id
        """
        if pending:
            query += " WHERE c.confirmed = FALSE"
        query += " ORDER BY c.similarity DESC"
        rows = [dict(r) for r in db.execute(query).fetchall()]

    if as_json or link:
        emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)
        return

    # Human-readable: one block per connection
    if not rows:
        print_info("No connections found.")
        return
    if n:
        rows = rows[:n]
    for row in rows:
        status = "[green]✓ confirmed[/green]" if row["confirmed"] else "[yellow]? pending[/yellow]"
        console.print(f"\n[bold]{row['stream_title']}[/bold]  →  [magenta]{row['pin_title']}[/magenta]  {status}")
        console.print(f"  [dim]id: {row['id']}  sim: {row['similarity']:.2f}  source: {row['source']}[/dim]")
        if row["llm_note"]:
            console.print(f"  [italic]{row['llm_note']}[/italic]")


# ---------------------------------------------------------------------------
# confirm / reject
# ---------------------------------------------------------------------------

@app.command()
def confirm(conn_id: str = typer.Argument(..., help="Connection ID to confirm")):
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
def reject(conn_id: str = typer.Argument(..., help="Connection ID to reject")):
    """Reject and delete a suggested connection."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            conn_mod.reject_connection(db, conn_id)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Connection {conn_id} rejected.")


# ---------------------------------------------------------------------------
# link
# ---------------------------------------------------------------------------

@app.command()
def link(
    stream_id: str = typer.Argument(...),
    pin_id: str = typer.Argument(...),
    note: Optional[str] = typer.Option(None, "--note", "-n"),
):
    """Manually link a stream to a pin (always confirmed)."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        try:
            cid = conn_mod.manual_link(db, stream_id, pin_id, note=note)
        except ValueError as e:
            print_error(str(e))
            raise typer.Exit(1)
    print_success(f"Linked  connection_id={cid}")


# ---------------------------------------------------------------------------
# lab
# ---------------------------------------------------------------------------

@app.command()
def lab(
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "-s", "--select"),
    n: int = typer.Option(20, "-n"),
):
    """Show unpinned streams gaining traction (by open-frequency score)."""
    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        rows = lab_scores(db, half_life_days=cfg.half_life_days, limit=n)
    emit(rows, link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# why
# ---------------------------------------------------------------------------

@app.command()
def why(stream_id: str = typer.Argument(..., help="Stream ID to inspect")):
    """Show the event timeline for a stream (raw causality)."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        stream = db.execute("SELECT title FROM streams WHERE id = ?", (stream_id,)).fetchone()
        if not stream:
            print_error(f"Stream {stream_id} not found.")
            raise typer.Exit(1)
        events = db.execute(
            "SELECT event_type, occurred_at, metadata_json FROM events WHERE stream_id = ? ORDER BY occurred_at",
            (stream_id,),
        ).fetchall()

    console.print(f"\n[bold]Timeline for:[/bold] {stream['title']}")
    for ev in events:
        meta = json.loads(ev["metadata_json"]) if ev["metadata_json"] else {}
        console.print(f"  [cyan]{ev['occurred_at']}[/cyan]  [bold]{ev['event_type']}[/bold]  {meta}")
    if not events:
        print_info("No events recorded for this stream yet.")


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------

edit_app = typer.Typer(help="Edit stream or pin fields in $EDITOR.")
app.add_typer(edit_app, name="edit")


@edit_app.callback(invoke_without_command=True)
def edit_stream(
    ctx: typer.Context,
    stream_id: Optional[str] = typer.Argument(None, help="Stream ID to edit"),
):
    """Edit a stream's editable fields (title, note)."""
    if ctx.invoked_subcommand:
        return
    if not stream_id:
        print_error("Provide a stream ID or use: pinboard edit pin <id>")
        raise typer.Exit(1)

    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        row = db.execute("SELECT * FROM streams WHERE id = ?", (stream_id,)).fetchone()
        if not row:
            print_error(f"Stream {stream_id} not found.")
            raise typer.Exit(1)

        buffer = {
            "title": row["title"],
            "note": row["note"] or "",
        }
        import yaml
        original = yaml.dump(buffer, allow_unicode=True)

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tf:
            tf.write(original)
            tmp_path = tf.name

        subprocess.run([cfg.effective_editor(), tmp_path])
        edited = Path(tmp_path).read_text()
        Path(tmp_path).unlink(missing_ok=True)

        try:
            updated = yaml.safe_load(edited)
        except yaml.YAMLError as e:
            print_error(f"Invalid YAML: {e}")
            raise typer.Exit(1)

        if updated == buffer:
            print_info("No changes.")
            return

        db.execute(
            "UPDATE streams SET title = ?, note = ? WHERE id = ?",
            (updated.get("title", row["title"]), updated.get("note") or None, stream_id),
        )
        record(db, "edit", stream_id=stream_id, metadata={"fields": list(updated.keys())})

    print_success(f"Stream {stream_id} updated.")


@edit_app.command("pin")
def edit_pin(
    id_or_slot: str = typer.Argument(..., help="Pin id, stream id, or slot number"),
):
    """Edit a pin's note in $EDITOR."""
    _ensure_init()
    cfg = Config.load()
    with get_conn(DB_PATH) as db:
        pin_id = pin_mod.resolve_pin_id(db, id_or_slot)
        if not pin_id:
            print_error(f"No active pin found for: {id_or_slot}")
            raise typer.Exit(1)

        row = db.execute("SELECT * FROM pins WHERE id = ?", (pin_id,)).fetchone()
        buffer = {"note": row["note"] or ""}
        import yaml
        original = yaml.dump(buffer, allow_unicode=True)

        with tempfile.NamedTemporaryFile(suffix=".yaml", mode="w", delete=False) as tf:
            tf.write(original)
            tmp_path = tf.name

        subprocess.run([cfg.effective_editor(), tmp_path])
        edited = Path(tmp_path).read_text()
        Path(tmp_path).unlink(missing_ok=True)

        try:
            updated = yaml.safe_load(edited)
        except yaml.YAMLError as e:
            print_error(f"Invalid YAML: {e}")
            raise typer.Exit(1)

        if updated == buffer:
            print_info("No changes.")
            return

        db.execute(
            "UPDATE pins SET note = ? WHERE id = ?",
            (updated.get("note") or None, pin_id),
        )
        record(db, "edit", pin_id=pin_id, metadata={"fields": ["note"]})

    print_success(f"Pin {pin_id} updated.")


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@app.command()
def search(
    query: str = typer.Argument(..., help="Semantic search query"),
    link: bool = typer.Option(False, "-l", "--link"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "-s", "--select"),
    n: int = typer.Option(10, "-n"),
):
    """Semantic search over all streams."""
    _ensure_init()
    cfg = Config.load()
    from .embeddings import build_service, deserialize, cosine_similarity
    embedder = build_service(cfg)
    if not embedder:
        print_error("No embedding service configured. Set openai_api_key in ~/.pinboard/config.toml")
        raise typer.Exit(1)

    query_vec = embedder.embed(query)

    with get_conn(DB_PATH) as db:
        rows = db.execute("SELECT id, title, kind, source, artifact_path, embedding FROM streams WHERE embedding IS NOT NULL").fetchall()

    results = []
    for row in rows:
        vec = deserialize(row["embedding"])
        sim = cosine_similarity(query_vec, vec)
        results.append({
            "id": row["id"],
            "title": row["title"],
            "kind": row["kind"],
            "source": row["source"],
            "artifact_path": row["artifact_path"],
            "similarity": round(sim, 4),
        })
    results.sort(key=lambda r: r["similarity"], reverse=True)
    emit(results[:n], link_only=link, as_json=as_json, pretty=pretty, select_fields=select, limit=n)


# ---------------------------------------------------------------------------
# graph
# ---------------------------------------------------------------------------

@app.command()
def graph(
    pin_id: Optional[str] = typer.Option(None, "--pin", help="Filter by pin id"),
    as_json: bool = typer.Option(False, "-j", "--json"),
    pretty: bool = typer.Option(False, "-P", "--pretty"),
    select: Optional[str] = typer.Option(None, "-s", "--select"),
    n: Optional[int] = typer.Option(None, "-n"),
):
    """Show the connection graph (ASCII or JSON)."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        q = """
            SELECT c.id, c.similarity, c.confirmed, c.source, c.llm_note,
                   s.title as stream_title, s.kind as stream_kind, s.source as stream_source,
                   s.artifact_path,
                   ps.title as pin_title, p.id as pin_id
            FROM connections c
            JOIN streams s ON s.id = c.stream_id
            JOIN pins p ON p.id = c.pin_id
            JOIN streams ps ON ps.id = p.stream_id
            WHERE c.confirmed = TRUE
        """
        params = []
        if pin_id:
            q += " AND c.pin_id = ?"
            params.append(pin_id)
        q += " ORDER BY c.similarity DESC"
        rows = [dict(r) for r in db.execute(q, params).fetchall()]

    if as_json:
        out = [_pick_select(r, select) for r in rows]
        indent = 2 if pretty else None
        print(json.dumps(out, indent=indent, default=str))
        return

    if not rows:
        print_info("No confirmed connections in the graph.")
        return

    # Group by pin
    by_pin: dict[str, list] = {}
    for r in rows:
        by_pin.setdefault(r["pin_title"], []).append(r)

    for pin_title, edges in by_pin.items():
        console.print(f"\n[bold magenta]★ {pin_title}[/bold magenta]")
        for e in edges:
            marker = "✓" if e["confirmed"] else "?"
            console.print(f"  [{marker}] {e['stream_title']} [dim](sim={e['similarity']:.2f} source={e['source']})[/dim]")
            if e["llm_note"]:
                console.print(f"      [italic]{e['llm_note']}[/italic]")


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
    fmt: str = typer.Option("json", "--format", "-f", help="Export format (json)"),
):
    """Export all data for backup."""
    _ensure_init()
    with get_conn(DB_PATH) as db:
        streams = [dict(r) for r in db.execute("SELECT * FROM streams").fetchall()]
        pins_rows = [dict(r) for r in db.execute("SELECT * FROM pins").fetchall()]
        conn_rows = [dict(r) for r in db.execute("SELECT * FROM connections").fetchall()]
        events_rows = [dict(r) for r in db.execute("SELECT * FROM events").fetchall()]

        # Strip binary embeddings for JSON export
        for s in streams:
            s.pop("embedding", None)

    out = {
        "streams": streams,
        "pins": pins_rows,
        "connections": conn_rows,
        "events": events_rows,
    }
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    app()
