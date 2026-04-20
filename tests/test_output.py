"""Tests for output formatters."""

import json
from io import StringIO
from unittest.mock import patch

import pytest

from pinboard.output import emit


SAMPLE_ROWS = [
    {"id": "abc", "title": "Hello", "kind": "url", "source": "https://example.com", "score": 0.9},
    {"id": "def", "title": "World", "kind": "pdf", "source": None, "artifact_path": "/tmp/x.pdf", "score": 0.4},
]


def test_json_output(capsys):
    emit(SAMPLE_ROWS, as_json=True)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data) == 2
    assert data[0]["id"] == "abc"


def test_json_select(capsys):
    emit(SAMPLE_ROWS, as_json=True, select_fields="id,score")
    data = json.loads(capsys.readouterr().out)
    assert list(data[0].keys()) == ["id", "score"]


def test_json_pretty_indent(capsys):
    emit(SAMPLE_ROWS, as_json=True, pretty=True)
    out = capsys.readouterr().out
    assert "\n" in out  # indented


def test_link_output(capsys):
    emit(SAMPLE_ROWS, link_only=True)
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0] == "https://example.com"
    assert lines[1] == "/tmp/x.pdf"


def test_limit(capsys):
    emit(SAMPLE_ROWS, as_json=True, limit=1)
    data = json.loads(capsys.readouterr().out)
    assert len(data) == 1


def test_empty_rows(capsys):
    emit([], as_json=True)
    data = json.loads(capsys.readouterr().out)
    assert data == []
