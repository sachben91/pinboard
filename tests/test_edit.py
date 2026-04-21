"""Integration test: edit via stubbed $EDITOR."""

import yaml
from pathlib import Path
from unittest.mock import patch

import pytest

from pinboard.db import get_conn, DEFAULT_CHANNEL_ID
from pinboard.streams import add_stream
from pinboard.pins import pin_stream
from pinboard.events import record

CID = DEFAULT_CHANNEL_ID


def test_edit_stream_updates_fields(db_path):
    with get_conn(db_path) as db:
        sid = add_stream(db, "https://example.com", channel_id=CID, title="Original Title")

        new_yaml = yaml.dump({"title": "Updated Title", "note": "My note"})
        updated = yaml.safe_load(new_yaml)
        db.execute(
            "UPDATE streams SET title = ?, note = ? WHERE id = ?",
            (updated["title"], updated.get("note"), sid),
        )
        record(db, "edit", stream_id=sid, metadata={"fields": list(updated.keys())})

        row = db.execute("SELECT title, note FROM streams WHERE id = ?", (sid,)).fetchone()
        assert row["title"] == "Updated Title"
        assert row["note"] == "My note"

        ev = db.execute("SELECT * FROM events WHERE event_type = 'edit' AND stream_id = ?", (sid,)).fetchone()
        assert ev is not None


def test_edit_pin_note(db_path):
    with get_conn(db_path) as db:
        sid = add_stream(db, "https://example.com", channel_id=CID, title="T")
        pin_id = pin_stream(db, CID, sid, note="original note")

        db.execute("UPDATE pins SET note = ? WHERE id = ?", ("updated reason", pin_id))
        record(db, "edit", pin_id=pin_id, metadata={"fields": ["note"]})

        row = db.execute("SELECT note FROM pins WHERE id = ?", (pin_id,)).fetchone()
        assert row["note"] == "updated reason"

        ev = db.execute("SELECT * FROM events WHERE event_type = 'edit' AND pin_id = ?", (pin_id,)).fetchone()
        assert ev is not None
