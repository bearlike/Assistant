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
* **SCG absent + seeding off** — a production install drops the demo fixtures
  from the catalog and resolves no tools, never a hardcoded guess (live
  configured MCP servers are covered in ``test_catalog_live_sources.py``).
* **Unconfigured demo source (seeding on)** — still returned, never omitted.

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
    """With demo seeding off, a demo source resolves to no tools.

    No hardcoded guess in production: ``tools_for`` is empty and ``entries``
    lists only configured MCP servers — the demo fixtures vanish entirely.
    """
    monkeypatch.setattr(catalog_mod, "seeding_enabled", lambda: False)
    assert SourceCatalog.tools_for(["filesystem"]) == []
    assert all(e.id != "filesystem" for e in SourceCatalog.entries())


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


# ── Grant inversion regression (run-797097e4b1) ──────────────────────────────


def _registry_with(specs):
    """A registry preloaded with *specs* (tool_id, server) — the live MCP set."""
    from mewbo_core.tool_registry import ToolRegistry, ToolSpec

    registry = ToolRegistry()
    for tool_id, server in specs:
        registry.register(
            ToolSpec(
                tool_id=tool_id,
                name=tool_id,
                description="",
                factory=lambda: None,  # type: ignore[arg-type, return-value]
                enabled=True,
                kind="mcp",
                metadata={"server": server, "tool": tool_id},
            )
        )
    return registry


def test_mapped_capability_mints_mcp_registry_id(monkeypatch) -> None:
    """A capability node's RAW name mints the ``mcp_<server>_<tool>`` registry id.

    The GRANT-INVERSION bug: a mapped source's capability ``name`` is the RAW MCP
    tool name (``search_repos``), so returning it made the ``filter_specs``
    intersection DELETE every mapped tool. It now mints ``mcp_tool_id`` so the
    grant matches the live registry id.
    """
    registry = _registry_with([("mcp_github_search_repos", "github")])
    monkeypatch.setattr(catalog_mod, "load_registry", lambda cwd=None: registry)
    store = scg_store.get_scg_store()
    # The node carries the RAW connector tool name, NOT the registry id.
    store.upsert_nodes([_capability("github", "search_repos")])
    assert SourceCatalog.tools_for(["github"]) == ["mcp_github_search_repos"]


def test_search_grant_drops_write_tools(monkeypatch) -> None:
    """A SEARCH grant is read-only: write-capable connector verbs are dropped.

    The EVIDENCE: a failed-map source bound ALL raw registry tools incl.
    ``create_repo`` / ``delete_branch``. ``tools_for`` now filters obvious write
    verbs while keeping every read verb.
    """
    registry = _registry_with(
        [
            ("mcp_gitea_get_file_contents", "gitea"),
            ("mcp_gitea_list_issues", "gitea"),
            ("mcp_gitea_search_repos", "gitea"),
            ("mcp_gitea_create_repo", "gitea"),
            ("mcp_gitea_delete_branch", "gitea"),
            ("mcp_gitea_create_or_update_file", "gitea"),
            ("mcp_gitea_fork_repo", "gitea"),
            ("mcp_gitea_wiki_write", "gitea"),
        ]
    )
    monkeypatch.setattr(catalog_mod, "load_registry", lambda cwd=None: registry)
    monkeypatch.setattr(
        catalog_mod, "get_merged_mcp_config",
        lambda project=None: {"servers": {"gitea": {}}},
    )
    granted = SourceCatalog.tools_for(["gitea"])
    # Read verbs kept ...
    assert "mcp_gitea_get_file_contents" in granted
    assert "mcp_gitea_list_issues" in granted
    assert "mcp_gitea_search_repos" in granted
    # ... write verbs dropped.
    for write in (
        "mcp_gitea_create_repo",
        "mcp_gitea_delete_branch",
        "mcp_gitea_create_or_update_file",
        "mcp_gitea_fork_repo",
        "mcp_gitea_wiki_write",
    ):
        assert write not in granted


def test_failed_map_source_falls_through_but_write_filtered(monkeypatch) -> None:
    """An unmapped (failed-map) live server grants its READ registry ids only.

    It falls through to the ``source_id in live`` branch (the EVIDENCE path that
    bound all 51 raw tools) — but the read-only filter now strips the mutators.
    """
    registry = _registry_with(
        [
            ("mcp_github_search_repositories", "github"),
            ("mcp_github_get_file_contents", "github"),
            ("mcp_github_create_issue", "github"),
            ("mcp_github_push_files", "github"),
            ("mcp_github_merge_pull_request", "github"),
        ]
    )
    monkeypatch.setattr(catalog_mod, "load_registry", lambda cwd=None: registry)
    monkeypatch.setattr(
        catalog_mod, "get_merged_mcp_config",
        lambda project=None: {"servers": {"github": {}}},
    )
    # No SCG nodes for github → falls through to the live registry ids.
    granted = SourceCatalog.tools_for(["github"])
    assert "mcp_github_search_repositories" in granted
    assert "mcp_github_get_file_contents" in granted
    assert "mcp_github_create_issue" not in granted
    assert "mcp_github_push_files" not in granted
    assert "mcp_github_merge_pull_request" not in granted
