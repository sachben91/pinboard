"""Tests for pin invariants."""

import pytest

from pinboard.db import DEFAULT_CHANNEL_ID
from pinboard.pins import pin_stream, unpin, active_pins, active_pin_count, MAX_PINS_PER_CHANNEL
from pinboard.streams import add_stream

CID = DEFAULT_CHANNEL_ID


def _add(db, n=1):
    ids = []
    for i in range(n):
        ids.append(add_stream(db, f"https://example.com/{i}", channel_id=CID, title=f"Stream {i}"))
    return ids


def test_pin_creates_row(db):
    (sid,) = _add(db)
    pin_id = pin_stream(db, CID, sid)
    pins = active_pins(db, CID)
    assert len(pins) == 1
    assert pins[0]["stream_id"] == sid


def test_pin_slots_sequential(db):
    sids = _add(db, 3)
    for sid in sids:
        pin_stream(db, CID, sid)
    pins = active_pins(db, CID)
    assert [p["slot_order"] for p in pins] == [1, 2, 3]


def test_max_pins_enforced(db):
    sids = _add(db, MAX_PINS_PER_CHANNEL + 1)
    for sid in sids[:MAX_PINS_PER_CHANNEL]:
        pin_stream(db, CID, sid)
    with pytest.raises(ValueError, match="3"):
        pin_stream(db, CID, sids[MAX_PINS_PER_CHANNEL])


def test_double_pin_rejected(db):
    (sid,) = _add(db)
    pin_stream(db, CID, sid)
    with pytest.raises(ValueError, match="already pinned"):
        pin_stream(db, CID, sid)


def test_unpin_by_slot(db):
    sids = _add(db, 3)
    for sid in sids:
        pin_stream(db, CID, sid)
    unpin(db, CID, "2")
    pins = active_pins(db, CID)
    assert len(pins) == 2
    assert [p["slot_order"] for p in pins] == [1, 2]


def test_unpin_renumbers(db):
    sids = _add(db, 3)
    for sid in sids:
        pin_stream(db, CID, sid)
    unpin(db, CID, "1")
    pins = active_pins(db, CID)
    assert [p["slot_order"] for p in pins] == [1, 2]


def test_unpin_all_allowed(db):
    sids = _add(db, 2)
    for sid in sids:
        pin_stream(db, CID, sid)
    unpin(db, CID, "1")
    unpin(db, CID, "1")
    assert active_pin_count(db, CID) == 0


def test_unpin_nonexistent_raises(db):
    with pytest.raises(ValueError):
        unpin(db, CID, "99")


def test_channels_isolated(db_path):
    """Pins in different channels don't interfere."""
    from pinboard.db import get_conn
    from pinboard.channels import create_channel

    with get_conn(db_path) as db:
        ch2 = create_channel(db, "work")
        s1 = add_stream(db, "https://a.com", channel_id=CID, title="A")
        s2 = add_stream(db, "https://b.com", channel_id=ch2, title="B")

        pin_stream(db, CID, s1)
        pin_stream(db, ch2, s2)

        assert active_pin_count(db, CID) == 1
        assert active_pin_count(db, ch2) == 1

        # Filling one channel doesn't affect the other
        for i in range(2):
            sid = add_stream(db, f"https://c{i}.com", channel_id=CID, title=f"C{i}")
            pin_stream(db, CID, sid)

        with pytest.raises(ValueError):
            extra = add_stream(db, "https://extra.com", channel_id=CID, title="Extra")
            pin_stream(db, CID, extra)

        # ch2 still has capacity
        s3 = add_stream(db, "https://d.com", channel_id=ch2, title="D")
        pin_stream(db, ch2, s3)  # should not raise
