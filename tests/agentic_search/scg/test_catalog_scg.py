"""SCG-resolution tests for :class:`SourceCatalog`.

``catalog.py`` resolves a selected source's ``allowed_tools`` from the live SCG
capability nodes for that source, intersected with ``filter_specs()`` registry
availability. Before a source is mapped it falls back to the illustrative
``tools`` declared in ``fixtures.SOURCE_CATALOG`` — but only while demo seeding
is enabled. These tests pin every branch:

* **SCG present** — capability nodes for a source drive ``tools_for`` /
  ``entries`` (∩ ``filter_specs``), overriding the demo fallback.
* **SCG absent + seeding on** — resolution falls back to the fixtures demo tools
  (still ∩ availability for ``tools_for``), without error.
* **SCG absent + seeding off** — a production install reports the source with
  ``available=False`` (honest "not indexed"), never a hardcoded guess.
* **Unconfigured source** — still returned, never omitted.

The store is the real JSON backend under a tmp dir (no MongoDB). ``filter_specs``
runs against the real built-in registry, so the intersection is asserted against
the *live* available ids rather than hard-coded names — ``read_file`` is a
guaranteed built-in we anchor on.
"""

from __future__ import annotations

import pytest
from mewbo_api.agentic_search import catalog as catalog_mod, fixtures
from mewbo_api.agentic_search.catalog import SourceCatalog
from mewbo_core.tool_registry import load_registry
from mewbo_graph.scg import store as scg_store
from mewbo_graph.scg.types import ScgNode

# A built-in tool id guaranteed to exist in the default registry, used as a
# capability-node name so the filter_specs() intersection keeps it.
_REAL_TOOL = "read_file"


@pytest.fixture(autouse=True)
def _reset_scg_store():
    """Fresh, empty JSON SCG store under a tmp dir for each test."""
    scg_store.reset_for_tests()
    yield
    scg_store.set_scg_store(None)


def _available_ids() -> set[str]:
    """The live registry's available tool ids (what filter_specs keeps)."""
    return {spec.tool_id for spec in load_registry().list_specs()}


def _demo(source_id: str) -> list[str]:
    """The illustrative demo tools declared beside a source in fixtures."""
    return next(
        list(r.get("tools", []))
        for r in fixtures.SOURCE_CATALOG
        if r["id"] == source_id
    )


def _capability(source_id: str, tool_name: str) -> ScgNode:
    """A capability node whose ``name`` is the concrete tool id it unlocks."""
    return ScgNode(
        source_key=f"{source_id}#{tool_name}",
        kind="capability",
        source_id=source_id,
        name=tool_name,
    )


# ── SCG present (overrides the demo fallback) ────────────────────────────────


def test_tools_for_resolves_from_scg_capability_nodes() -> None:
    """When capability nodes exist for a source, tools_for returns their names.

    The result is the capability names ∩ live registry availability — anchored
    on ``read_file``, a guaranteed built-in.
    """
    assert _REAL_TOOL in _available_ids()  # guard the anchor assumption
    store = scg_store.get_scg_store()
    store.upsert_nodes([_capability("filesystem", _REAL_TOOL)])
    assert SourceCatalog.tools_for(["filesystem"]) == [_REAL_TOOL]


def test_tools_for_intersects_with_filter_specs() -> None:
    """A capability naming a tool absent from the registry is dropped."""
    store = scg_store.get_scg_store()
    store.upsert_nodes(
        [
            _capability("filesystem", _REAL_TOOL),
            _capability("filesystem", "totally_not_a_real_tool_xyz"),
        ]
    )
    tools = SourceCatalog.tools_for(["filesystem"])
    assert _REAL_TOOL in tools
    assert "totally_not_a_real_tool_xyz" not in tools


def test_entries_enriches_tool_ids_from_scg() -> None:
    """entries() reflects SCG capability nodes in tool_ids when present.

    entries() is the console-facing inventory: it surfaces the source's resolved
    tools as-is (no registry intersection, so an unbacked tool still shows greyed
    in the UI), with SCG capability nodes overriding the demo fallback.
    """
    store = scg_store.get_scg_store()
    store.upsert_nodes([_capability("filesystem", _REAL_TOOL)])
    entry = next(e for e in SourceCatalog.entries() if e.id == "filesystem")
    assert entry.tool_ids == [_REAL_TOOL]
    assert entry.available is True


def test_only_capability_kind_nodes_unlock_tools() -> None:
    """Non-capability nodes (entity_type/field) never contribute tool ids."""
    store = scg_store.get_scg_store()
    store.upsert_nodes(
        [
            ScgNode(
                source_key="filesystem#File",
                kind="entity_type",
                source_id="filesystem",
                name=_REAL_TOOL,  # name collides with a real tool — must NOT leak
            )
        ]
    )
    # No capability nodes → falls back to the fixtures demo tools (seeding on by
    # default), never the entity node's name.
    entry = next(e for e in SourceCatalog.entries() if e.id == "filesystem")
    assert entry.tool_ids == _demo("filesystem")


# ── SCG absent + seeding on (demo fallback) ──────────────────────────────────


def test_tools_for_falls_back_to_demo_when_seeding_on() -> None:
    """With no SCG nodes + seeding on, tools_for uses the demo tools ∩ availability."""
    available = _available_ids()
    expected = [t for t in _demo("filesystem") if t in available]
    assert SourceCatalog.tools_for(["filesystem"]) == expected


def test_tools_for_per_source_fallback_is_independent() -> None:
    """A source with SCG nodes resolves from SCG; another falls back, same call."""
    store = scg_store.get_scg_store()
    store.upsert_nodes([_capability("filesystem", _REAL_TOOL)])
    tools = SourceCatalog.tools_for(["filesystem", "github"])
    # filesystem resolves from the SCG capability node (∩ availability).
    assert _REAL_TOOL in tools
    # github has no SCG nodes → its demo fallback names are considered (none
    # survive the registry intersection here, which is correct).
    available = _available_ids()
    for tool_id in _demo("github"):
        assert (tool_id in tools) == (tool_id in available)


def test_tools_for_unknown_source_is_skipped() -> None:
    """An id absent from the catalog contributes nothing and never raises."""
    assert SourceCatalog.tools_for(["does-not-exist"]) == []


# ── SCG absent + seeding off (honest production path) ─────────────────────────


def test_seeding_off_unmapped_source_has_no_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With demo seeding off, an unmapped source resolves to no tools.

    No hardcoded guess in production: ``tools_for`` is empty and ``entries``
    reports the source ``available=False``.
    """
    monkeypatch.setattr(catalog_mod, "seeding_enabled", lambda: False)
    assert SourceCatalog.tools_for(["filesystem"]) == []
    entry = next(e for e in SourceCatalog.entries() if e.id == "filesystem")
    assert entry.available is False
    assert entry.unavailable_reason
    assert entry.tool_ids == []


def test_seeding_off_scg_still_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seeding off only disables the demo fallback — real SCG tools still resolve."""
    monkeypatch.setattr(catalog_mod, "seeding_enabled", lambda: False)
    store = scg_store.get_scg_store()
    store.upsert_nodes([_capability("filesystem", _REAL_TOOL)])
    assert SourceCatalog.tools_for(["filesystem"]) == [_REAL_TOOL]


# ── Unconfigured sources stay in the catalog, never omitted ───────────────────


def test_entries_keeps_all_catalog_sources() -> None:
    """Every fixture source is present even with an empty SCG."""
    ids = {e.id for e in SourceCatalog.entries()}
    assert {"notion", "github", "filesystem", "web"} <= ids
