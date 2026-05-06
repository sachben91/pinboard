"""Tests for engagement scoring."""

import math
from datetime import datetime, timezone, timedelta

import pytest

from pinboard.db import DEFAULT_STREAM_ID, get_conn
from pinboard.scoring import link_score, lab_scores

SID = DEFAULT_STREAM_ID


def test_score_no_opens(db):
    from pinboard.links import add_link
    lid = add_link(db, "https://example.com", stream_id=SID, title="T")
    assert link_score(db, lid, half_life_days=14.0) == 0.0


def test_score_single_fresh_open(db):
    from pinboard.links import add_link
    lid = add_link(db, "https://example.com", stream_id=SID, title="T")
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (lid, datetime.now(timezone.utc).isoformat()),
    )
    assert abs(link_score(db, lid, half_life_days=14.0) - 1.0) < 0.01


def test_score_decays_at_half_life(db):
    from pinboard.links import add_link
    lid = add_link(db, "https://example.com", stream_id=SID, title="T")
    ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (lid, ts),
    )
    assert abs(link_score(db, lid, half_life_days=14.0) - math.exp(-1)) < 0.02


def test_lab_ordering(prebuilt_db):
    path, l1, l2, l3 = prebuilt_db
    with get_conn(path) as conn:
        results = lab_scores(conn, stream_id=SID, half_life_days=14.0, limit=10)

    ids = [r["id"] for r in results]
    assert ids.index(l1) < ids.index(l2)
    assert ids.index(l2) < ids.index(l3)


def test_lab_excludes_pinned(prebuilt_db):
    path, l1, l2, l3 = prebuilt_db
    with get_conn(path) as conn:
        from pinboard.pins import create_pin, add_link_to_pin
        pin_id = create_pin(conn, SID, "Focus")
        add_link_to_pin(conn, pin_id, l1)
        results = lab_scores(conn, stream_id=SID, half_life_days=14.0, limit=10)

    assert l1 not in [r["id"] for r in results]
