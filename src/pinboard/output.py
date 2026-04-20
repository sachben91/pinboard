"""Output formatting: human table, --json, --link, --select."""

from __future__ import annotations

import json
import sys
from typing import Any


def _pick(row: dict, fields: list[str] | None) -> dict:
    if not fields:
        return row
    return {k: row[k] for k in fields if k in row}


def _link_value(row: dict) -> str | None:
    """Return the primary linkable value for a row."""
    return row.get("source") or row.get("artifact_path") or row.get("id")


def emit(
    rows: list[dict[str, Any]],
    *,
    as_json: bool = False,
    pretty: bool = False,
    link_only: bool = False,
    select_fields: str | None = None,
    limit: int | None = None,
) -> None:
    fields = [f.strip() for f in select_fields.split(",")] if select_fields else None
    if limit:
        rows = rows[:limit]

    if link_only:
        for row in rows:
            val = _link_value(row)
            if val:
                print(val)
        return

    if as_json:
        out = [_pick(r, fields) for r in rows]
        indent = 2 if pretty else None
        print(json.dumps(out, indent=indent, default=str))
        return

    # Human-readable table via rich
    from rich.console import Console
    from rich.table import Table

    console = Console()
    if not rows:
        console.print("[dim]No results.[/dim]")
        return

    display_fields = fields or list(rows[0].keys())
    table = Table(show_header=True, header_style="bold cyan", box=None, pad_edge=False)
    for f in display_fields:
        table.add_column(f, overflow="fold", max_width=60)

    for row in rows:
        picked = _pick(row, display_fields)
        table.add_row(*[str(picked.get(f, "")) for f in display_fields])

    console.print(table)


def print_error(msg: str) -> None:
    from rich.console import Console
    Console(stderr=True).print(f"[bold red]Error:[/bold red] {msg}")


def print_success(msg: str) -> None:
    from rich.console import Console
    Console().print(f"[bold green]✓[/bold green] {msg}")


def print_info(msg: str) -> None:
    from rich.console import Console
    Console().print(f"[dim]{msg}[/dim]")
