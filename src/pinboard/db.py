"""SQLite/PostgreSQL database initialization and connection management.

When the DATABASE_URL environment variable is set, uses PostgreSQL via psycopg2.
Otherwise falls back to SQLite for local dev and tests.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path


# ---------------------------------------------------------------------------
# PostgreSQL support (optional import)
# ---------------------------------------------------------------------------

try:
    import psycopg2
    import psycopg2.extras

    HAS_PG = True
except ImportError:
    HAS_PG = False


# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

SCHEMA_VERSION = 7

# SQLite DDL — kept fully intact for local dev and migrations
DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS streams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS links (
    id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL REFERENCES streams(id),
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    source TEXT,
    artifact_path TEXT,
    content_text TEXT,
    note TEXT,
    tags TEXT,
    created_at TIMESTAMP NOT NULL,
    posted_at TIMESTAMP,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS pins (
    id TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL REFERENCES streams(id),
    name TEXT NOT NULL,
    note TEXT,
    slot_order INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL,
    closed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pin_links (
    id TEXT PRIMARY KEY,
    pin_id TEXT NOT NULL REFERENCES pins(id),
    link_id TEXT NOT NULL REFERENCES links(id),
    added_at TIMESTAMP NOT NULL,
    UNIQUE(pin_id, link_id)
);

CREATE TABLE IF NOT EXISTS pin_skills (
    id TEXT PRIMARY KEY,
    pin_id TEXT NOT NULL REFERENCES pins(id),
    themes TEXT NOT NULL,
    questions TEXT NOT NULL,
    adjacent TEXT NOT NULL,
    search_signals TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pin_skills_pin ON pin_skills(pin_id);

CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    link_id TEXT NOT NULL REFERENCES links(id),
    pin_id TEXT NOT NULL REFERENCES pins(id),
    similarity REAL NOT NULL,
    llm_note TEXT,
    confirmed BOOLEAN DEFAULT FALSE,
    source TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(link_id, pin_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    channel_id TEXT,
    stream_id TEXT,
    pin_id TEXT,
    metadata_json TEXT,
    occurred_at TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_pins_active ON pins(closed_at) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_links_stream ON links(stream_id);
CREATE INDEX IF NOT EXISTS idx_pins_stream ON pins(stream_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_pin_links_pin ON pin_links(pin_id);
CREATE INDEX IF NOT EXISTS idx_pin_links_link ON pin_links(link_id);

CREATE TABLE IF NOT EXISTS discord_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL UNIQUE,
    link_id TEXT NOT NULL REFERENCES links(id),
    channel_id TEXT NOT NULL,
    stream_id TEXT NOT NULL REFERENCES streams(id),
    posted_at TIMESTAMP NOT NULL,
    resolved_at TIMESTAMP,
    outcome TEXT
);

CREATE INDEX IF NOT EXISTS idx_discord_reviews_link ON discord_reviews(link_id);
CREATE INDEX IF NOT EXISTS idx_discord_reviews_stream ON discord_reviews(stream_id);
"""

# PostgreSQL DDL — BLOB→BYTEA, AUTOINCREMENT→BIGSERIAL, no partial index
PG_DDL = """
CREATE TABLE IF NOT EXISTS streams (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS links (id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(id), kind TEXT NOT NULL, title TEXT NOT NULL, source TEXT, artifact_path TEXT, content_text TEXT, note TEXT, tags TEXT, created_at TIMESTAMP NOT NULL, posted_at TIMESTAMP, embedding BYTEA);
CREATE TABLE IF NOT EXISTS pins (id TEXT PRIMARY KEY, stream_id TEXT NOT NULL REFERENCES streams(id), name TEXT NOT NULL, note TEXT, slot_order INTEGER NOT NULL, created_at TIMESTAMP NOT NULL, closed_at TIMESTAMP);
CREATE TABLE IF NOT EXISTS pin_links (id TEXT PRIMARY KEY, pin_id TEXT NOT NULL REFERENCES pins(id), link_id TEXT NOT NULL REFERENCES links(id), added_at TIMESTAMP NOT NULL, UNIQUE(pin_id, link_id));
CREATE TABLE IF NOT EXISTS pin_skills (id TEXT PRIMARY KEY, pin_id TEXT NOT NULL REFERENCES pins(id), themes TEXT NOT NULL, questions TEXT NOT NULL, adjacent TEXT NOT NULL, search_signals TEXT NOT NULL, created_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS connections (id TEXT PRIMARY KEY, link_id TEXT NOT NULL REFERENCES links(id), pin_id TEXT NOT NULL REFERENCES pins(id), similarity REAL NOT NULL, llm_note TEXT, confirmed BOOLEAN DEFAULT FALSE, source TEXT NOT NULL, created_at TIMESTAMP NOT NULL, UNIQUE(link_id, pin_id));
CREATE TABLE IF NOT EXISTS events (id BIGSERIAL PRIMARY KEY, event_type TEXT NOT NULL, channel_id TEXT, stream_id TEXT, pin_id TEXT, metadata_json TEXT, occurred_at TIMESTAMP NOT NULL);
CREATE TABLE IF NOT EXISTS discord_reviews (id BIGSERIAL PRIMARY KEY, message_id TEXT NOT NULL UNIQUE, link_id TEXT NOT NULL, channel_id TEXT NOT NULL, stream_id TEXT NOT NULL, posted_at TIMESTAMP NOT NULL, resolved_at TIMESTAMP, outcome TEXT);
CREATE INDEX IF NOT EXISTS idx_events_stream ON events(stream_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_pins_stream ON pins(stream_id, closed_at);
CREATE INDEX IF NOT EXISTS idx_links_stream ON links(stream_id);
CREATE INDEX IF NOT EXISTS idx_pin_links_pin ON pin_links(pin_id);
CREATE INDEX IF NOT EXISTS idx_pin_links_link ON pin_links(link_id);
CREATE INDEX IF NOT EXISTS idx_discord_reviews_link ON discord_reviews(link_id);
CREATE INDEX IF NOT EXISTS idx_discord_reviews_stream ON discord_reviews(stream_id);
"""

DEFAULT_STREAM_ID = "00000000-0000-0000-0000-000000000001"
DEFAULT_STREAM_NAME = "default"


# ---------------------------------------------------------------------------
# PostgreSQL compatibility wrappers
# ---------------------------------------------------------------------------


class _CompatRow(dict):
    """A dict subclass that also supports integer index access.

    psycopg2 with RealDictCursor returns plain dicts, but application code
    uses ``row[0]`` for scalar results (e.g. COUNT queries).  This class
    bridges both access patterns.
    """

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _PGCursor:
    """Wraps a psycopg2 cursor so that fetchone/fetchall return _CompatRow."""

    def __init__(self, cursor):
        self._cur = cursor

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        return _CompatRow(row)

    def fetchall(self):
        return [_CompatRow(r) for r in self._cur.fetchall()]

    # Proxy everything else transparently
    def __getattr__(self, name):
        return getattr(self._cur, name)


def _convert_sql(sql: str) -> str:
    """Convert SQLite-style SQL to PostgreSQL-compatible SQL.

    Transformations applied:
    - ``?``  →  ``%s``  (parameter placeholder)
    - ``INSERT OR IGNORE INTO tablename``  →  ``INSERT INTO tablename``
      with ``ON CONFLICT DO NOTHING`` appended before any trailing semicolon
    """
    import re

    # Replace ? placeholders with %s
    sql = sql.replace("?", "%s")

    # Handle INSERT OR IGNORE INTO  →  INSERT INTO ... ON CONFLICT DO NOTHING
    pattern = re.compile(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", re.IGNORECASE)
    if pattern.search(sql):
        sql = pattern.sub("INSERT INTO", sql)
        # Strip trailing semicolon, append ON CONFLICT DO NOTHING, restore semicolon
        stripped = sql.rstrip()
        if stripped.endswith(";"):
            sql = stripped[:-1].rstrip() + " ON CONFLICT DO NOTHING;"
        else:
            sql = stripped + " ON CONFLICT DO NOTHING"

    return sql


class _PGConn:
    """Wraps a psycopg2 connection with an SQLite-compatible interface.

    Provides ``.execute()`` and ``.executemany()`` that auto-convert SQLite
    syntax (``?`` params, ``INSERT OR IGNORE``) to PostgreSQL syntax.
    Also wraps ``.cursor()`` to return ``_PGCursor`` instances.
    """

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql: str, params=None):
        sql = _convert_sql(sql)
        cur = self._conn.cursor()
        if params is None:
            cur.execute(sql)
        else:
            cur.execute(sql, params)
        return _PGCursor(cur)

    def executemany(self, sql: str, params_list):
        sql = _convert_sql(sql)
        cur = self._conn.cursor()
        cur.executemany(sql, params_list)
        return _PGCursor(cur)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    # Proxy anything else (e.g. .cursor()) to the underlying connection
    def __getattr__(self, name):
        return getattr(self._conn, name)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _get_database_url() -> str | None:
    """Return DATABASE_URL from env, normalising postgres:// → postgresql://."""
    url = os.environ.get("DATABASE_URL")
    if url and url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def init_db(db_path: Path | None = None) -> None:
    """Initialise the database schema.

    Uses PostgreSQL when DATABASE_URL is set, otherwise SQLite at *db_path*.
    """
    database_url = _get_database_url()

    if database_url:
        if not HAS_PG:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg2 is not installed. "
                "Install it with: pip install psycopg2-binary"
            )
        pg_conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            conn = _PGConn(pg_conn)
            for stmt in PG_DDL.split(";"):
                stmt = stmt.strip()
                if stmt:
                    conn.execute(stmt)
            conn.execute(
                "INSERT INTO streams (id, name, created_at) VALUES (%s, %s, NOW()) ON CONFLICT DO NOTHING",
                (DEFAULT_STREAM_ID, DEFAULT_STREAM_NAME),
            )
            pg_conn.commit()
        finally:
            pg_conn.close()
        return

    # SQLite path (unchanged)
    if db_path is None:
        raise ValueError("db_path is required when DATABASE_URL is not set")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)")
        conn.commit()

        row = conn.execute("SELECT version FROM schema_version").fetchone()
        if row is None:
            conn.executescript(DDL)
            conn.execute(
                "INSERT OR IGNORE INTO streams (id, name, created_at) VALUES (?, ?, datetime('now'))",
                (DEFAULT_STREAM_ID, DEFAULT_STREAM_NAME),
            )
            conn.execute("INSERT OR IGNORE INTO schema_version VALUES (?)", (SCHEMA_VERSION,))
        elif row[0] < SCHEMA_VERSION:
            _migrate(conn, row[0])
            conn.executescript(DDL)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
        else:
            conn.executescript(DDL)
        conn.commit()


@contextmanager
def get_conn(db_path: Path | None = None):
    """Context-manager that yields a database connection.

    Yields a ``_PGConn`` when DATABASE_URL is set, otherwise a standard
    ``sqlite3.Connection`` with ``row_factory = sqlite3.Row``.
    """
    database_url = _get_database_url()

    if database_url:
        if not HAS_PG:
            raise RuntimeError(
                "DATABASE_URL is set but psycopg2 is not installed. "
                "Install it with: pip install psycopg2-binary"
            )
        pg_conn = psycopg2.connect(database_url, cursor_factory=psycopg2.extras.RealDictCursor)
        conn = _PGConn(pg_conn)
        try:
            yield conn
            pg_conn.commit()
        except Exception:
            pg_conn.rollback()
            raise
        finally:
            pg_conn.close()
        return

    # SQLite path (unchanged)
    if db_path is None:
        raise ValueError("db_path is required when DATABASE_URL is not set")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# SQLite migration helpers (unchanged, SQLite-only)
# ---------------------------------------------------------------------------


def _migrate(conn: sqlite3.Connection, from_version: int) -> None:
    if from_version < 2:
        _migrate_v1_to_v2(conn)
    if from_version < 3:
        _migrate_v2_to_v3(conn)
    if from_version < 4:
        _migrate_v3_to_v4(conn)
    if from_version < 5:
        _migrate_v4_to_v5(conn)
    if from_version < 6:
        _migrate_v5_to_v6(conn)
    if from_version < 7:
        _migrate_v6_to_v7(conn)


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    stmts = [
        "CREATE TABLE IF NOT EXISTS channels (id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TIMESTAMP NOT NULL)",
        f"INSERT OR IGNORE INTO channels (id, name, created_at) VALUES ('{DEFAULT_STREAM_ID}', 'default', datetime('now'))",
        "ALTER TABLE streams ADD COLUMN channel_id TEXT REFERENCES channels(id)",
        f"UPDATE streams SET channel_id = '{DEFAULT_STREAM_ID}' WHERE channel_id IS NULL",
        "ALTER TABLE pins ADD COLUMN channel_id TEXT REFERENCES channels(id)",
        f"UPDATE pins SET channel_id = '{DEFAULT_STREAM_ID}' WHERE channel_id IS NULL",
        "ALTER TABLE events ADD COLUMN channel_id TEXT",
        f"UPDATE events SET channel_id = '{DEFAULT_STREAM_ID}' WHERE channel_id IS NULL",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError as e:
            if "duplicate column" in str(e).lower():
                continue
            raise


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    stmts = [
        "CREATE TABLE IF NOT EXISTS pin_skills (id TEXT PRIMARY KEY, pin_id TEXT NOT NULL REFERENCES pins(id), themes TEXT NOT NULL, questions TEXT NOT NULL, adjacent TEXT NOT NULL, search_signals TEXT NOT NULL, created_at TIMESTAMP NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_pin_skills_pin ON pin_skills(pin_id)",
    ]
    for stmt in stmts:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Rename channels→streams, streams→links, transform pins to cluster model, add pin_links."""
    conn.execute("PRAGMA foreign_keys = OFF")

    # Step 1: rename old streams (content items) to a temp name to free up 'streams'
    conn.execute("ALTER TABLE streams RENAME TO links_temp")

    # Step 2: rename channels (workspaces) to streams
    conn.execute("ALTER TABLE channels RENAME TO streams")

    # Step 3: create links with proper stream_id column name (was channel_id)
    conn.execute("""
        CREATE TABLE links (
            id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT,
            artifact_path TEXT,
            content_text TEXT,
            note TEXT,
            created_at TIMESTAMP NOT NULL,
            embedding BLOB
        )
    """)
    conn.execute("""
        INSERT INTO links (id, stream_id, kind, title, source, artifact_path, content_text, note, created_at, embedding)
        SELECT id, channel_id, kind, title, source, artifact_path, content_text, note, created_at, embedding
        FROM links_temp
    """)
    conn.execute("DROP TABLE links_temp")

    # Step 4: create new pins table with cluster model
    conn.execute("""
        CREATE TABLE pins_new (
            id TEXT PRIMARY KEY,
            stream_id TEXT NOT NULL,
            name TEXT NOT NULL,
            note TEXT,
            slot_order INTEGER NOT NULL,
            created_at TIMESTAMP NOT NULL,
            closed_at TIMESTAMP
        )
    """)
    # Derive cluster name from the linked content item's title (truncated to 40 chars)
    conn.execute("""
        INSERT INTO pins_new (id, stream_id, name, note, slot_order, created_at, closed_at)
        SELECT p.id, p.channel_id,
               COALESCE(SUBSTR(l.title, 1, 40), 'Pin ' || p.slot_order),
               p.note, p.slot_order, p.pinned_at, p.unpinned_at
        FROM pins p
        LEFT JOIN links l ON l.id = p.stream_id
    """)

    # Step 5: create pin_links — one entry per old single-link pin
    conn.execute("""
        CREATE TABLE pin_links (
            id TEXT PRIMARY KEY,
            pin_id TEXT NOT NULL,
            link_id TEXT NOT NULL,
            added_at TIMESTAMP NOT NULL,
            UNIQUE(pin_id, link_id)
        )
    """)
    old_pins = conn.execute("SELECT id, stream_id, pinned_at FROM pins WHERE stream_id IS NOT NULL").fetchall()
    for pin_id, link_id, pinned_at in old_pins:
        conn.execute(
            "INSERT OR IGNORE INTO pin_links (id, pin_id, link_id, added_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), pin_id, link_id, pinned_at),
        )

    conn.execute("DROP TABLE pins")
    conn.execute("ALTER TABLE pins_new RENAME TO pins")

    # Step 6: rebuild connections with link_id column name (was stream_id)
    conn.execute("""
        CREATE TABLE connections_new (
            id TEXT PRIMARY KEY,
            link_id TEXT NOT NULL,
            pin_id TEXT NOT NULL,
            similarity REAL NOT NULL,
            llm_note TEXT,
            confirmed BOOLEAN DEFAULT FALSE,
            source TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            UNIQUE(link_id, pin_id)
        )
    """)
    conn.execute("""
        INSERT INTO connections_new (id, link_id, pin_id, similarity, llm_note, confirmed, source, created_at)
        SELECT id, stream_id, pin_id, similarity, llm_note, confirmed, source, created_at
        FROM connections
    """)
    conn.execute("DROP TABLE connections")
    conn.execute("ALTER TABLE connections_new RENAME TO connections")

    conn.execute("PRAGMA foreign_keys = ON")


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE links ADD COLUMN posted_at TIMESTAMP")
    except sqlite3.OperationalError:
        pass


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ALTER TABLE links ADD COLUMN tags TEXT")
    except sqlite3.OperationalError:
        pass


def _migrate_v6_to_v7(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS discord_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE,
            link_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            stream_id TEXT NOT NULL,
            posted_at TIMESTAMP NOT NULL,
            resolved_at TIMESTAMP,
            outcome TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_discord_reviews_link ON discord_reviews(link_id);
        CREATE INDEX IF NOT EXISTS idx_discord_reviews_stream ON discord_reviews(stream_id);
    """)
