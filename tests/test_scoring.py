"""Tests for engagement scoring."""

import math
from datetime import datetime, timezone, timedelta

import pytest

from pinboard.scoring import stream_score, lab_scores
from pinboard.db import get_conn


def test_score_no_opens(db, cfg):
    """Stream with no opens scores 0."""
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", title="T")
    assert stream_score(db, sid, half_life_days=14.0) == 0.0


def test_score_single_fresh_open(db):
    """Stream opened right now scores ~1.0."""
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", title="T")
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (sid, datetime.now(timezone.utc).isoformat()),
    )
    score = stream_score(db, sid, half_life_days=14.0)
    assert abs(score - 1.0) < 0.01


def test_score_decays_at_half_life(db):
    """Score at exactly half_life_days should be ~0.5."""
    from pinboard.streams import add_stream
    sid = add_stream(db, "https://example.com", title="T")
    ts = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    db.execute(
        "INSERT INTO events (event_type, stream_id, occurred_at) VALUES ('open', ?, ?)",
        (sid, ts),
    )
    score = stream_score(db, sid, half_life_days=14.0)
    assert abs(score - math.exp(-1)) < 0.02


def test_lab_ordering(prebuilt_db):
    """Lab returns streams in descending score order."""
    path, s1, s2, s3 = prebuilt_db
    with get_conn(path) as conn:
        results = lab_scores(conn, half_life_days=14.0, limit=10)

    ids = [r["id"] for r in results]
    # s1 (3 recent opens) > s2 (1 open at half-life) > s3 (1 open at 30d)
    assert ids.index(s1) < ids.index(s2)
    assert ids.index(s2) < ids.index(s3)


def test_lab_excludes_pinned(prebuilt_db):
    """Lab should not include currently pinned streams."""
    path, s1, s2, s3 = prebuilt_db
    with get_conn(path) as conn:
        from pinboard.pins import pin_stream
        pin_stream(conn, s1)
        results = lab_scores(conn, half_life_days=14.0, limit=10)

    ids = [r["id"] for r in results]
    assert s1 not in ids
