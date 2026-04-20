"""Tests for pin invariants."""

import pytest

from pinboard.pins import pin_stream, unpin, active_pins, active_pin_count, MAX_PINS
from pinboard.streams import add_stream


def _add(db, n=1):
    ids = []
    for i in range(n):
        ids.append(add_stream(db, f"https://example.com/{i}", title=f"Stream {i}"))
    return ids


def test_pin_creates_row(db):
    (sid,) = _add(db)
    pin_id = pin_stream(db, sid)
    pins = active_pins(db)
    assert len(pins) == 1
    assert pins[0]["stream_id"] == sid


def test_pin_slots_sequential(db):
    sids = _add(db, 3)
    for sid in sids:
        pin_stream(db, sid)
    pins = active_pins(db)
    slots = [p["slot_order"] for p in pins]
    assert slots == [1, 2, 3]


def test_max_pins_enforced(db):
    sids = _add(db, MAX_PINS + 1)
    for sid in sids[:MAX_PINS]:
        pin_stream(db, sid)
    with pytest.raises(ValueError, match="maximum"):
        pin_stream(db, sids[MAX_PINS])


def test_double_pin_rejected(db):
    (sid,) = _add(db)
    pin_stream(db, sid)
    with pytest.raises(ValueError, match="already pinned"):
        pin_stream(db, sid)


def test_unpin_by_slot(db):
    sids = _add(db, 3)
    for sid in sids:
        pin_stream(db, sid)
    unpin(db, "2")
    pins = active_pins(db)
    assert len(pins) == 2
    slots = [p["slot_order"] for p in pins]
    assert slots == [1, 2]  # re-numbered


def test_unpin_renumbers(db):
    sids = _add(db, 3)
    pin_ids = [pin_stream(db, sid) for sid in sids]
    unpin(db, "1")  # remove slot 1
    pins = active_pins(db)
    assert [p["slot_order"] for p in pins] == [1, 2]


def test_unpin_all_allowed(db):
    sids = _add(db, 2)
    for sid in sids:
        pin_stream(db, sid)
    unpin(db, "1")
    unpin(db, "1")
    assert active_pin_count(db) == 0


def test_unpin_nonexistent_raises(db):
    with pytest.raises(ValueError):
        unpin(db, "99")
