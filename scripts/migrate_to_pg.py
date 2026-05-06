#!/usr/bin/env python3
"""Migrate all data from a local SQLite Pinboard DB to PostgreSQL.

Usage:
    DATABASE_URL=postgresql://user:pass@host:5432/dbname python3 scripts/migrate_to_pg.py

The script reads DATABASE_URL from the environment and the SQLite DB from the
Google Drive path used by the bot, falling back to ~/.pinboard/pinboard.db.

Tables are copied in dependency order so that foreign-key constraints are
satisfied without disabling them on the PG side.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("ERROR: psycopg2 is not installed.  Run: pip install psycopg2-binary", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Locate the SQLite DB
# ---------------------------------------------------------------------------

GDRIVE_PATH = Path.home() / "Library/CloudStorage/GoogleDrive-sachben91@gmail.com/My Drive/pinboard/pinboard.db"
FALLBACK_PATH = Path("/data/pinboard.db")


def find_sqlite_db() -> Path:
    if GDRIVE_PATH.exists():
        return GDRIVE_PATH
    if FALLBACK_PATH.exists():
        return FALLBACK_PATH
    # Allow override via env (same var the bot uses)
    env_path = os.environ.get("PINBOARD_DB_PATH")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
    print(
        f"ERROR: Cannot find SQLite DB.  Tried:\n"
        f"  {GDRIVE_PATH}\n"
        f"  {FALLBACK_PATH}\n"
        f"  PINBOARD_DB_PATH env var (not set)\n"
        f"Set PINBOARD_DB_PATH to override.",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Table definitions: (table_name, columns, has_embedding)
# Ordered so that parent tables come before child tables.
# ---------------------------------------------------------------------------

TABLES = [
    # name,             columns (in SELECT order),                                         embedding_col
    ("streams",         "id, name, created_at",                                             False),
    ("links",           "id, stream_id, kind, title, source, artifact_path, content_text, "
                        "note, tags, created_at, posted_at, embedding",                     True),
    ("pins",            "id, stream_id, name, note, slot_order, created_at, closed_at",     False),
    ("pin_links",       "id, pin_id, link_id, added_at",                                    False),
    ("pin_skills",      "id, pin_id, themes, questions, adjacent, search_signals, created_at", False),
    ("connections",     "id, link_id, pin_id, similarity, llm_note, confirmed, source, created_at", False),
    ("events",          "event_type, channel_id, stream_id, pin_id, metadata_json, occurred_at", False),
    ("discord_reviews", "message_id, link_id, channel_id, stream_id, posted_at, resolved_at, outcome", False),
]

# events and discord_reviews have BIGSERIAL PKs managed by PG — we omit `id`
# from the INSERT and let the sequence assign one.

# Columns that are INTEGER in SQLite but BOOLEAN in PostgreSQL
BOOL_COLUMNS: set[str] = {"confirmed"}


def build_insert(table: str, columns: str) -> str:
    col_list = [c.strip() for c in columns.split(",")]
    placeholders = ", ".join(["%s"] * len(col_list))
    return f"INSERT INTO {table} ({columns}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    columns: str,
    has_embedding: bool,
) -> None:
    col_list = [c.strip() for c in columns.split(",")]
    embedding_idx = col_list.index("embedding") if has_embedding else -1

    # For tables with FK references, skip orphaned rows
    if table == "connections":
        query = (
            f"SELECT {columns} FROM {table} "
            f"WHERE link_id IN (SELECT id FROM links) "
            f"AND pin_id IN (SELECT id FROM pins)"
        )
    elif table == "discord_reviews":
        query = (
            f"SELECT {columns} FROM {table} "
            f"WHERE link_id IN (SELECT id FROM links)"
        )
    else:
        query = f"SELECT {columns} FROM {table}"
    rows = sqlite_conn.execute(query).fetchall()
    if not rows:
        print(f"  {table}: 0 rows (skipping)")
        return

    insert_sql = build_insert(table, columns)
    bool_indices = [i for i, c in enumerate(col_list) if c in BOOL_COLUMNS]

    batch = []
    for row in rows:
        row_data = list(row)
        if embedding_idx >= 0 and row_data[embedding_idx] is not None:
            row_data[embedding_idx] = psycopg2.Binary(row_data[embedding_idx])
        for i in bool_indices:
            if row_data[i] is not None:
                row_data[i] = bool(row_data[i])
        batch.append(tuple(row_data))

    cur = pg_conn.cursor()
    cur.executemany(insert_sql, batch)
    print(f"  {table}: {len(batch)} rows inserted")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        print("ERROR: DATABASE_URL environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    # Railway uses postgres:// scheme; psycopg2 needs postgresql://
    if database_url.startswith("postgres://"):
        database_url = "postgresql://" + database_url[len("postgres://"):]

    sqlite_path = find_sqlite_db()
    print(f"Source SQLite DB : {sqlite_path}")
    print(f"Target PostgreSQL: {database_url[:40]}...")

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    pg_conn = psycopg2.connect(database_url)

    print("\nCreating schema...")
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent.parent))
        from src.pinboard.db import PG_DDL
        cur = pg_conn.cursor()
        for stmt in PG_DDL.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        pg_conn.commit()
        print("  Schema ready.")
    except Exception as e:
        pg_conn.rollback()
        print(f"ERROR creating schema: {e}", file=sys.stderr)
        raise

    print("\nMigrating tables...")
    try:
        for table, columns, has_embedding in TABLES:
            migrate_table(sqlite_conn, pg_conn, table, columns, has_embedding)
        pg_conn.commit()
        print("\nMigration complete.")
    except Exception:
        pg_conn.rollback()
        raise
    finally:
        sqlite_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    main()
