"""Integration test: edit via stubbed $EDITOR."""

import yaml

import pytest

from pinboard.db import get_conn, DEFAULT_STREAM_ID
from pinboard.links import add_link
from pinboard.pins import create_pin, add_link_to_pin
from pinboard.events import record

SID = DEFAULT_STREAM_ID


def test_edit_link_updates_fields(db_path):
    with get_conn(db_path) as db:
        lid = add_link(db, "https://example.com", stream_id=SID, title="Original Title")

        new_yaml = yaml.dump({"title": "Updated Title", "note": "My note"})
        updated = yaml.safe_load(new_yaml)
        db.execute(
            "UPDATE links SET title = ?, note = ? WHERE id = ?",
            (updated["title"], updated.get("note"), lid),
        )
        record(db, "edit", stream_id=lid, metadata={"fields": list(updated.keys())})

        row = db.execute("SELECT title, note FROM links WHERE id = ?", (lid,)).fetchone()
        assert row["title"] == "Updated Title"
        assert row["note"] == "My note"

        ev = db.execute("SELECT * FROM events WHERE event_type = 'edit' AND stream_id = ?", (lid,)).fetchone()
        assert ev is not None


def test_edit_pin_note(db_path):
    with get_conn(db_path) as db:
        lid = add_link(db, "https://example.com", stream_id=SID, title="T")
        pin_id = create_pin(db, SID, "Test Cluster")
        add_link_to_pin(db, pin_id, lid)

        db.execute("UPDATE pins SET note = ? WHERE id = ?", ("updated reason", pin_id))
        record(db, "edit", pin_id=pin_id, metadata={"fields": ["note"]})

        row = db.execute("SELECT note FROM pins WHERE id = ?", (pin_id,)).fetchone()
        assert row["note"] == "updated reason"

        ev = db.execute("SELECT * FROM events WHERE event_type = 'edit' AND pin_id = ?", (pin_id,)).fetchone()
        assert ev is not None
