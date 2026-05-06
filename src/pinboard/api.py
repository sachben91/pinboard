"""FastAPI web server for pinboard."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import DB_PATH
from .db import get_conn
from .links import prepare_link, write_link
from .pins import (
    active_pins, create_pin, close_pin,
    add_link_to_pin, remove_link_from_pin, resolve_pin_id,
)
from .scoring import lab_scores
from .streams_ws import list_streams, resolve_stream_id

app = FastAPI(title="Pinboard")

_STATIC = Path(__file__).parent / "static"


# ---------------------------------------------------------------------------
# Pydantic request bodies
# ---------------------------------------------------------------------------

class CreatePinBody(BaseModel):
    name: str
    note: Optional[str] = None


class AddLinkBody(BaseModel):
    link_id: str


class IngestLinkBody(BaseModel):
    url: str
    title: Optional[str] = None
    note: Optional[str] = None


class PatchLinkBody(BaseModel):
    tags: Optional[list[str]] = None
    title: Optional[str] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# Stream endpoints
# ---------------------------------------------------------------------------

@app.get("/api/streams")
def get_streams():
    with get_conn(DB_PATH) as db:
        return list_streams(db)


@app.get("/api/streams/{stream_id}/links")
def get_links(stream_id: str, page: int = 1, q: str = "", tag: str = "", limit: int = 20):
    import json
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        offset = (page - 1) * limit

        filters = "l.stream_id = ?"
        params: list = [stream_id]
        if q:
            filters += " AND (l.title LIKE ? OR l.source LIKE ? OR l.content_text LIKE ?)"
            params += [f"%{q}%", f"%{q}%", f"%{q}%"]
        if tag:
            filters += " AND l.tags LIKE ?"
            params.append(f'%"{tag}"%')

        rows = db.execute(
            f"""
            SELECT l.id, l.title, l.source, l.kind, l.created_at, l.posted_at,
                   l.note, l.tags, l.content_text
            FROM links l
            WHERE {filters}
            ORDER BY COALESCE(l.posted_at, l.created_at) DESC
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()
        total = db.execute(
            f"SELECT COUNT(*) FROM links l WHERE {filters}", params
        ).fetchone()[0]

        pinned_ids = {
            r["link_id"] for r in db.execute(
                """
                SELECT pl.link_id FROM pin_links pl
                JOIN pins p ON p.id = pl.pin_id
                WHERE p.stream_id = ? AND p.closed_at IS NULL
                """,
                (stream_id,),
            ).fetchall()
        }

        items = []
        for row in rows:
            text = row["content_text"] or ""
            summary = text[:300].strip() if text else ""
            if len(text) > 300:
                summary += "…"
            raw_tags = row["tags"]
            tags = json.loads(raw_tags) if raw_tags else []
            items.append({
                "id": row["id"],
                "title": row["title"],
                "source": row["source"],
                "kind": row["kind"],
                "created_at": row["created_at"],
                "posted_at": row["posted_at"],
                "note": row["note"],
                "tags": tags,
                "summary": summary,
                "pinned": row["id"] in pinned_ids,
            })

        return {"items": items, "total": total, "page": page, "limit": limit}


@app.patch("/api/links/{link_id}")
def patch_link(link_id: str, body: PatchLinkBody):
    import json
    with get_conn(DB_PATH) as db:
        row = db.execute("SELECT id FROM links WHERE id = ?", (link_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Link not found.")
        if body.tags is not None:
            db.execute("UPDATE links SET tags = ? WHERE id = ?",
                       (json.dumps(body.tags), link_id))
        if body.title is not None:
            db.execute("UPDATE links SET title = ? WHERE id = ?", (body.title, link_id))
        if body.note is not None:
            db.execute("UPDATE links SET note = ? WHERE id = ?", (body.note, link_id))
    return {"ok": True}


@app.post("/api/streams/{stream_id}/links", status_code=201)
def post_link(stream_id: str, body: IngestLinkBody):
    # Fetch stream name first (quick read)
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        row = db.execute("SELECT name FROM streams WHERE id = ?", (stream_id,)).fetchone()
        stream_name = row["name"]

    # prepare_link does network I/O — run outside the DB connection
    prepared = prepare_link(
        body.url, stream_name=stream_name, stream_id=stream_id,
        title=body.title or None, note=body.note or None,
    )

    with get_conn(DB_PATH) as db:
        link_id = write_link(db, prepared)

    return {"id": link_id, "title": prepared["title"]}


@app.get("/api/streams/{stream_id}/pins")
def get_pins(stream_id: str):
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        return active_pins(db, stream_id)


@app.post("/api/streams/{stream_id}/pins", status_code=201)
def post_pin(stream_id: str, body: CreatePinBody):
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        try:
            pin_id = create_pin(db, stream_id, body.name, body.note)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": pin_id}


@app.delete("/api/streams/{stream_id}/pins/{pin_id}", status_code=200)
def delete_pin(stream_id: str, pin_id: str):
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        try:
            close_pin(db, stream_id, pin_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}


@app.get("/api/streams/{stream_id}/lab")
def get_lab(stream_id: str, limit: int = 20):
    with get_conn(DB_PATH) as db:
        _assert_stream(db, stream_id)
        return lab_scores(db, stream_id=stream_id, limit=limit)


# ---------------------------------------------------------------------------
# Pin link endpoints
# ---------------------------------------------------------------------------

@app.post("/api/pins/{pin_id}/links", status_code=201)
def post_pin_link(pin_id: str, body: AddLinkBody):
    with get_conn(DB_PATH) as db:
        pin = db.execute("SELECT id FROM pins WHERE id = ? AND closed_at IS NULL", (pin_id,)).fetchone()
        if not pin:
            raise HTTPException(status_code=404, detail=f"Pin {pin_id} not found or closed.")
        try:
            pl_id = add_link_to_pin(db, pin_id, body.link_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        return {"id": pl_id}


@app.delete("/api/pins/{pin_id}/links/{link_id}", status_code=200)
def delete_pin_link(pin_id: str, link_id: str):
    with get_conn(DB_PATH) as db:
        try:
            remove_link_from_pin(db, pin_id, link_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
        return {"ok": True}


# ---------------------------------------------------------------------------
# Static files + SPA fallback
# ---------------------------------------------------------------------------

@app.get("/")
def index():
    return FileResponse(_STATIC / "index.html")


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _assert_stream(db, stream_id: str) -> None:
    row = db.execute("SELECT id FROM streams WHERE id = ?", (stream_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"Stream {stream_id} not found.")
