"""Tests for engagement scoring."""

import math
from datetime import datetime, timezone, timedelta

import pytest

from pinboard.db import DEFAULT_CHANNEL_ID, get_conn
from pinboard.scoring import stream_score, lab_scores

CID = DEFAULT_CHANNEL_ID


def test_score_no_opens(db):
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", channel_id=CID, title="T")
    assert stream_score(db, sid, half_life_days=14.0) == 0.0


def test_score_single_fresh_open(db):
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", channel_id=CID, title="T")
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (sid, datetime.now(timezone.utc).isoformat()),
    )
    assert abs(stream_score(db, sid, half_life_days=14.0) - 1.0) < 0.01


def test_score_decays_at_half_life(db):
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", channel_id=CID, title="T")
    ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (sid, ts),
    )
    assert abs(stream_score(db, sid, half_life_days=14.0) - math.exp(-1)) < 0.02


def test_lab_ordering(prebuilt_db):
    path, s1, s2, s3 = prebuilt_db
    with get_conn(path) as conn:
        results = lab_scores(conn, channel_id=CID, half_life_days=14.0, limit=10)

    ids = [r["id"] for r in results]
    assert ids.index(s1) < ids.index(s2)
    assert ids.index(s2) < ids.index(s3)


def test_lab_excludes_pinned(prebuilt_db):
    path, s1, s2, s3 = prebuilt_db
    with get_conn(path) as conn:
        from pinboard.pins import pin_stream
        pin_stream(conn, CID, s1)
        results = lab_scores(conn, channel_id=CID, half_life_days=14.0, limit=10)

    assert s1 not in [r["id"] for r in results]
