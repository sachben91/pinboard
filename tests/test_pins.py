"""Tests for pin cluster invariants."""

import pytest

from pinboard.db import DEFAULT_STREAM_ID, get_conn
from pinboard.pins import (
    create_pin, close_pin, add_link_to_pin, remove_link_from_pin,
    active_pins, active_pin_count, resolve_pin_id,
    MAX_PINS_PER_STREAM, MAX_LINKS_PER_PIN,
)
from pinboard.links import add_link

SID = DEFAULT_STREAM_ID


def _add_link(db, n=1):
    ids = []
    for i in range(n):
        ids.append(add_link(db, f"https://example.com/{i}", stream_id=SID, title=f"Link {i}"))
    return ids


def test_create_pin(db):
    pin_id = create_pin(db, SID, "My Cluster")
    pins = active_pins(db, SID)
    assert len(pins) == 1
    assert pins[0]["name"] == "My Cluster"
    assert pins[0]["id"] == pin_id


def test_pin_slots_sequential(db):
    for i in range(MAX_PINS_PER_STREAM):
        create_pin(db, SID, f"Cluster {i}")
    pins = active_pins(db, SID)
    assert [p["slot_order"] for p in pins] == [1, 2, 3]


def test_max_pins_enforced(db):
    for i in range(MAX_PINS_PER_STREAM):
        create_pin(db, SID, f"Cluster {i}")
    with pytest.raises(ValueError, match=str(MAX_PINS_PER_STREAM)):
        create_pin(db, SID, "One too many")


def test_add_link_to_pin(db):
    pin_id = create_pin(db, SID, "Focus")
    (lid,) = _add_link(db)
    add_link_to_pin(db, pin_id, lid)
    pins = active_pins(db, SID)
    assert len(pins[0]["links"]) == 1
    assert pins[0]["links"][0]["id"] == lid


def test_max_links_per_pin_enforced(db):
    pin_id = create_pin(db, SID, "Focus")
    lids = _add_link(db, MAX_LINKS_PER_PIN + 1)
    for lid in lids[:MAX_LINKS_PER_PIN]:
        add_link_to_pin(db, pin_id, lid)
    with pytest.raises(ValueError, match=str(MAX_LINKS_PER_PIN)):
        add_link_to_pin(db, pin_id, lids[MAX_LINKS_PER_PIN])


def test_duplicate_link_rejected(db):
    pin_id = create_pin(db, SID, "Focus")
    (lid,) = _add_link(db)
    add_link_to_pin(db, pin_id, lid)
    with pytest.raises(ValueError, match="already in"):
        add_link_to_pin(db, pin_id, lid)


def test_remove_link_from_pin(db):
    pin_id = create_pin(db, SID, "Focus")
    (lid,) = _add_link(db)
    add_link_to_pin(db, pin_id, lid)
    remove_link_from_pin(db, pin_id, lid)
    pins = active_pins(db, SID)
    assert pins[0]["links"] == []


def test_close_pin_by_slot(db):
    for i in range(3):
        create_pin(db, SID, f"Cluster {i}")
    close_pin(db, SID, "2")
    pins = active_pins(db, SID)
    assert len(pins) == 2
    assert [p["slot_order"] for p in pins] == [1, 2]


def test_close_pin_renumbers(db):
    for i in range(3):
        create_pin(db, SID, f"Cluster {i}")
    close_pin(db, SID, "1")
    pins = active_pins(db, SID)
    assert [p["slot_order"] for p in pins] == [1, 2]


def test_close_all_allowed(db):
    create_pin(db, SID, "A")
    create_pin(db, SID, "B")
    close_pin(db, SID, "1")
    close_pin(db, SID, "1")
    assert active_pin_count(db, SID) == 0


def test_close_nonexistent_raises(db):
    with pytest.raises(ValueError):
        close_pin(db, SID, "99")


def test_streams_isolated(db_path):
    """Pins in different streams don't interfere."""
    from pinboard.db import get_conn
    from pinboard.streams_ws import create_stream

    with get_conn(db_path) as db:
        sid2 = create_stream(db, "work")

        create_pin(db, SID, "Focus A")
        create_pin(db, sid2, "Focus B")

        assert active_pin_count(db, SID) == 1
        assert active_pin_count(db, sid2) == 1

        # Fill stream 1
        create_pin(db, SID, "Focus C")
        create_pin(db, SID, "Focus D")

        with pytest.raises(ValueError):
            create_pin(db, SID, "Too many")

        # stream 2 still has capacity
        create_pin(db, sid2, "Focus E")  # should not raise
