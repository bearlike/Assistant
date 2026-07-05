#!/usr/bin/env python3
"""Tests for PromptHistory (issue #155, epic #149)."""

from __future__ import annotations

from mewbo_cli.tui.input.history import PromptHistory


def _hist(tmp_path, cap=1000):
    return PromptHistory(path=tmp_path / "hist", cap=cap)


def test_append_and_persist_roundtrip(tmp_path):
    h = _hist(tmp_path)
    h.append("first")
    h.append("second")
    # A fresh instance reads the persisted file.
    h2 = _hist(tmp_path)
    assert h2.entries() == ["first", "second"]


def test_append_skips_blanks_and_immediate_dupes(tmp_path):
    h = _hist(tmp_path)
    h.append("a")
    h.append("a")  # immediate dup ignored
    h.append("   ")  # blank ignored
    h.append("b")
    assert h.entries() == ["a", "b"]


def test_cap_truncates_oldest(tmp_path):
    h = _hist(tmp_path, cap=3)
    for line in ["1", "2", "3", "4"]:
        h.append(line)
    assert h.entries() == ["2", "3", "4"]


def test_recent_is_newest_first(tmp_path):
    h = _hist(tmp_path)
    for line in ["a", "b", "c"]:
        h.append(line)
    assert h.recent() == ["c", "b", "a"]
    assert h.recent(limit=2) == ["c", "b"]


def test_search_newest_first_dedup(tmp_path):
    h = _hist(tmp_path)
    for line in ["run tests", "fix bug", "run lint", "run tests again"]:
        h.append(line)
    results = h.search("run")
    assert results == ["run tests again", "run lint", "run tests"]


def test_search_casefold(tmp_path):
    h = _hist(tmp_path)
    h.append("Hello World")
    assert h.search("hello") == ["Hello World"]


def test_search_empty_query_returns_recent(tmp_path):
    h = _hist(tmp_path)
    for line in ["a", "b"]:
        h.append(line)
    assert h.search("") == ["b", "a"]


def test_best_match(tmp_path):
    h = _hist(tmp_path)
    for line in ["deploy prod", "deploy stage"]:
        h.append(line)
    assert h.best_match("deploy") == "deploy stage"
    assert h.best_match("nope") is None


def test_missing_file_is_empty(tmp_path):
    h = PromptHistory(path=tmp_path / "does_not_exist", cap=10)
    assert h.entries() == []
