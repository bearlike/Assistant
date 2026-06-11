#!/usr/bin/env python3
"""Tests for capabilities.py — parse_capabilities + filter_by_capabilities."""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from mewbo_core.capabilities import (
    augment_session_capabilities,
    filter_by_capabilities,
    parse_capabilities,
    register_session_capability_provider,
    reset_session_capability_providers,
)

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


# ------------------------------------------------------------------
# augment_session_capabilities + the provider push registry (#83-B)
# ------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_providers():
    """Isolate the global provider registry per test (a process-wide singleton)."""
    reset_session_capability_providers()
    yield
    reset_session_capability_providers()


def test_augment_no_providers_returns_advertised_unchanged():
    assert augment_session_capabilities(("wiki",)) == ("wiki",)
    assert augment_session_capabilities(()) == ()


def test_augment_unions_provider_grant():
    register_session_capability_provider(
        lambda adv: ("scg",) if "scg" not in adv else ()
    )
    # An unscoped/plain session (advertised nothing) gains the runtime grant.
    assert augment_session_capabilities(()) == ("scg",)
    # Unioned + sorted + deduped with an existing advertisement.
    assert augment_session_capabilities(("wiki",)) == ("scg", "wiki")


def test_augment_provider_noops_when_already_advertised():
    seen: list[tuple[str, ...]] = []

    def provider(adv: tuple[str, ...]) -> tuple[str, ...]:
        seen.append(adv)
        return () if "scg" in adv else ("scg",)

    register_session_capability_provider(provider)
    # Already advertised → provider sees it and returns no extra (no double-grant).
    assert augment_session_capabilities(("scg",)) == ("scg",)
    assert seen == [("scg",)]


def test_augment_skips_raising_provider():
    def boom(_adv: tuple[str, ...]) -> tuple[str, ...]:
        raise RuntimeError("predicate unreachable store")

    register_session_capability_provider(lambda _adv: ("scg",))
    register_session_capability_provider(boom)
    # A raising provider is logged + skipped; the healthy grant still lands.
    assert augment_session_capabilities(()) == ("scg",)


def test_register_provider_is_idempotent_on_identity():
    def provider(_adv: tuple[str, ...]) -> tuple[str, ...]:
        return ("scg",)

    register_session_capability_provider(provider)
    register_session_capability_provider(provider)  # same identity → not added twice
    # Grant present exactly once (dedupe also makes a double-add invisible, but
    # the registry itself must not accumulate duplicates).
    assert augment_session_capabilities(()) == ("scg",)
