"""Link ingestion: detect kind, extract text, copy artifacts."""

from __future__ import annotations

import re
import shutil
import sqlite3
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .config import ARTIFACTS_DIR
from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


def _stream_dir(stream_id: str, conn: sqlite3.Connection) -> Path:
    """Return artifacts/<stream-name>/, creating it if needed."""
    row = conn.execute("SELECT name FROM streams WHERE id = ?", (stream_id,)).fetchone()
    name = re.sub(r"[^\w\-]", "-", (row["name"] if row else stream_id)).lower()
    path = ARTIFACTS_DIR / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def _detect_kind(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        return "url"
    p = Path(source)
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
        return "image"
    return "doc"


def _fetch_url(url: str, artifact_dir: Path, cache: bool = False) -> tuple[str, str | None, str | None]:
    """Returns (title, content_text, artifact_path)."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        content = trafilatura.extract(downloaded) if downloaded else None
        metadata = trafilatura.extract_metadata(downloaded) if downloaded else None
        title = (metadata.title if metadata and metadata.title else None) or url
        artifact_path = None
        if cache and downloaded:
            artifact_path = str(artifact_dir / f"{_new_id()}.html")
            Path(artifact_path).write_text(downloaded, encoding="utf-8")
        return title, content, artifact_path
    except Exception:
        return url, None, None


def _ingest_pdf(source: str, artifact_dir: Path) -> tuple[str, str | None, str]:
    artifact_path = str(artifact_dir / f"{_new_id()}.pdf")
    shutil.copy2(source, artifact_path)
    content = None
    try:
        from pypdf import PdfReader
        reader = PdfReader(source)
        pages = [p.extract_text() or "" for p in reader.pages]
        content = "\n".join(pages).strip() or None
    except Exception:
        pass
    return Path(source).stem, content, artifact_path


def _ingest_image(source: str, artifact_dir: Path) -> tuple[str, str]:
    suffix = Path(source).suffix
    artifact_path = str(artifact_dir / f"{_new_id()}{suffix}")
    shutil.copy2(source, artifact_path)
    return Path(source).stem, artifact_path


def prepare_link(
    source: str,
    *,
    stream_name: str,
    stream_id: str,
    title: str | None = None,
    note: str | None = None,
    posted_at: str | None = None,
    cache: bool = False,
    embedder=None,
) -> dict:
    """Fetch, extract, and embed a source. No DB writes — safe to call in parallel threads."""
    link_id = _new_id()
    kind = _detect_kind(source)
    artifact_path = None
    content_text = None
    embedding_blob = None

    stream_slug = re.sub(r"[^\w\-]", "-", stream_name).lower()
    artifact_dir = ARTIFACTS_DIR / stream_slug
    artifact_dir.mkdir(parents=True, exist_ok=True)

    if kind == "url":
        fetched_title, content_text, artifact_path = _fetch_url(source, artifact_dir, cache=cache)
        title = title or fetched_title
    elif kind == "pdf":
        fetched_title, content_text, artifact_path = _ingest_pdf(source, artifact_dir)
        title = title or fetched_title
    elif kind == "image":
        fetched_title, artifact_path = _ingest_image(source, artifact_dir)
        title = title or fetched_title
    else:
        p = Path(source)
        if p.exists():
            content_text = p.read_text(errors="replace")
            title = title or p.stem
        else:
            content_text = source
            kind = "note"
            title = title or (source[:40] + "…" if len(source) > 40 else source)

    if content_text and embedder:
        try:
            vec = embedder.embed(content_text[:8000])
            from .embeddings import serialize
            embedding_blob = serialize(vec)
        except Exception:
            pass

    return {
        "link_id": link_id,
        "stream_id": stream_id,
        "kind": kind,
        "title": title or source,
        "source": source,
        "artifact_path": artifact_path,
        "content_text": content_text,
        "note": note,
        "posted_at": posted_at,
        "embedding": embedding_blob,
    }


def write_link(conn: sqlite3.Connection, prepared: dict) -> str:
    """Write a prepared link dict to the DB. Returns link_id."""
    conn.execute(
        """
        INSERT INTO links (id, stream_id, kind, title, source, artifact_path, content_text, note, created_at, posted_at, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prepared["link_id"], prepared["stream_id"], prepared["kind"],
            prepared["title"], prepared["source"], prepared["artifact_path"],
            prepared["content_text"], prepared["note"], now_utc(),
            prepared.get("posted_at"), prepared["embedding"],
        ),
    )
    record(conn, "add", stream_id=prepared["link_id"],
           metadata={"kind": prepared["kind"], "source": prepared["source"], "channel_id": prepared["stream_id"]})
    return prepared["link_id"]


def add_link(
    conn: sqlite3.Connection,
    source: str,
    *,
    stream_id: str,
    title: str | None = None,
    note: str | None = None,
    cache: bool = False,
    embedder=None,
) -> str:
    """Insert a new link row; return its id."""
    stream_row = conn.execute("SELECT name FROM streams WHERE id = ?", (stream_id,)).fetchone()
    stream_name = stream_row["name"] if stream_row else stream_id
    prepared = prepare_link(
        source, stream_name=stream_name, stream_id=stream_id,
        title=title, note=note, cache=cache, embedder=embedder,
    )
    return write_link(conn, prepared)
