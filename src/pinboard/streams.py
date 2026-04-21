"""Stream ingestion: detect kind, extract text, copy artifacts."""

from __future__ import annotations

import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .config import ARTIFACTS_DIR
from .events import record, now_utc


def _new_id() -> str:
    return str(uuid.uuid4())


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


def _fetch_url(url: str, cache: bool = False) -> tuple[str, str | None, str | None]:
    """Returns (title, content_text, artifact_path)."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        content = trafilatura.extract(downloaded) if downloaded else None
        metadata = trafilatura.extract_metadata(downloaded) if downloaded else None
        title = (metadata.title if metadata and metadata.title else None) or url
        artifact_path = None
        if cache and downloaded:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            aid = _new_id()
            artifact_path = str(ARTIFACTS_DIR / f"{aid}.html")
            Path(artifact_path).write_text(downloaded, encoding="utf-8")
        return title, content, artifact_path
    except Exception:
        return url, None, None


def _ingest_pdf(source: str) -> tuple[str, str | None, str]:
    """Returns (title, content_text, artifact_path)."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    aid = _new_id()
    artifact_path = str(ARTIFACTS_DIR / f"{aid}.pdf")
    shutil.copy2(source, artifact_path)

    content = None
    try:
        from pypdf import PdfReader
        reader = PdfReader(source)
        pages = [p.extract_text() or "" for p in reader.pages]
        content = "\n".join(pages).strip() or None
    except Exception:
        pass

    title = Path(source).stem
    return title, content, artifact_path


def _ingest_image(source: str) -> tuple[str, str]:
    """Returns (title, artifact_path)."""
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(source).suffix
    aid = _new_id()
    artifact_path = str(ARTIFACTS_DIR / f"{aid}{suffix}")
    shutil.copy2(source, artifact_path)
    return Path(source).stem, artifact_path


def add_stream(
    conn: sqlite3.Connection,
    source: str,
    *,
    channel_id: str,
    title: str | None = None,
    note: str | None = None,
    cache: bool = False,
    embedder=None,
) -> str:
    """Insert a new stream row; return its id."""
    stream_id = _new_id()
    kind = _detect_kind(source)
    artifact_path = None
    content_text = None
    embedding_blob = None

    if kind == "url":
        fetched_title, content_text, artifact_path = _fetch_url(source, cache=cache)
        title = title or fetched_title
    elif kind == "pdf":
        fetched_title, content_text, artifact_path = _ingest_pdf(source)
        title = title or fetched_title
    elif kind == "image":
        fetched_title, artifact_path = _ingest_image(source)
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

    conn.execute(
        """
        INSERT INTO streams (id, channel_id, kind, title, source, artifact_path, content_text, note, created_at, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (stream_id, channel_id, kind, title or source, source, artifact_path, content_text, note, now_utc(), embedding_blob),
    )
    record(conn, "add", stream_id=stream_id,
           metadata={"kind": kind, "source": source, "channel_id": channel_id})
    return stream_id
