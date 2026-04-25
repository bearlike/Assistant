#!/usr/bin/env python3
"""Tests for capabilities.py — parse_capabilities + filter_by_capabilities."""

from __future__ import annotations

from dataclasses import dataclass

from meeseeks_core.capabilities import filter_by_capabilities, parse_capabilities

# ------------------------------------------------------------------
# parse_capabilities
# ------------------------------------------------------------------


def test_parse_capabilities_none_returns_empty_tuple():
    assert parse_capabilities(None) == ()


def test_parse_capabilities_empty_string_returns_empty_tuple():
    assert parse_capabilities("") == ()


def test_parse_capabilities_whitespace_string_returns_empty_tuple():
    assert parse_capabilities("   ") == ()


def test_parse_capabilities_scalar_string_is_trimmed_and_wrapped():
    assert parse_capabilities("  stlite  ") == ("stlite",)


def test_parse_capabilities_empty_list_returns_empty_tuple():
    assert parse_capabilities([]) == ()


def test_parse_capabilities_dedupes_duplicates():
    assert parse_capabilities(["stlite", "stlite"]) == ("stlite",)


def test_parse_capabilities_sorts_list():
    assert parse_capabilities(["b", "a"]) == ("a", "b")


def test_parse_capabilities_filters_empty_and_whitespace_entries():
    assert parse_capabilities([None, "", "  "]) == ()


def test_parse_capabilities_unknown_shape_returns_empty_tuple():
    assert parse_capabilities(42) == ()
    assert parse_capabilities({"not": "a list"}) == ()


# ------------------------------------------------------------------
# filter_by_capabilities
# ------------------------------------------------------------------


@dataclass(frozen=True)
class _Item:
    name: str
    requires_capabilities: tuple[str, ...] = ()


def test_filter_by_capabilities_empty_requires_always_visible():
    items = [_Item("free"), _Item("gated", ("stlite",))]
    result = filter_by_capabilities(items, ())
    assert [i.name for i in result] == ["free"]


def test_filter_by_capabilities_subset_is_visible():
    items = [_Item("a", ("stlite",)), _Item("b", ("stlite", "charts"))]
    result = filter_by_capabilities(items, ("stlite", "charts", "extra"))
    assert [i.name for i in result] == ["a", "b"]


def test_filter_by_capabilities_missing_capability_hidden():
    items = [_Item("needs_charts", ("charts",))]
    result = filter_by_capabilities(items, ("stlite",))
    assert result == []


def test_filter_by_capabilities_session_ordering_irrelevant():
    items = [_Item("x", ("a", "b"))]
    assert filter_by_capabilities(items, ("a", "b")) == items
    assert filter_by_capabilities(items, ("b", "a")) == items
