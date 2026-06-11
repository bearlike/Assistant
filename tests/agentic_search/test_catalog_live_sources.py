"""Live-source resolution tests for :class:`SourceCatalog`.

``catalog.py`` now lists the **configured MCP servers** (the merged
``configs/mcp.json`` chain, read through the tool registry) as first-class
sources, merging the demo fixtures only while seeding is on. These tests pin
the merge + resolution rules with the two I/O boundaries stubbed (the merged
MCP config read and the registry load) — everything else (grouping,
``filter_specs`` intersection, entry shaping) runs the real code path.

* **Live server, tools discovered** — appears as an available entry (id =
  server name, ``source_type="mcp_tool_list"``) whose ``tool_ids`` are the
  registry's ``mcp_<server>_*`` ids; ``tools_for`` grants them.
* **Live server, discovery failed** — still listed, ``available=False`` with
  the manifest's ``disabled_reason`` surfaced.
* **Seeding gate** — demo fixtures merge after the live entries only while
  seeding is on; a live server id always wins a fixture-id collision.
* **SCG override** — capability nodes for a live server beat its registry ids.
"""

from __future__ import annotations

import pytest
from mewbo_api.agentic_search import catalog as catalog_mod
from mewbo_api.agentic_search.catalog import SourceCatalog
from mewbo_core.tool_registry import ToolRegistry, ToolSpec
from mewbo_graph.scg import store as scg_store
from mewbo_graph.scg.types import ScgNode


@pytest.fixture(autouse=True)
def _reset_scg_store():
    """Fresh, empty JSON SCG store under a tmp dir for each test."""
    scg_store.reset_for_tests()
    yield
    scg_store.set_scg_store(None)


def _spec(
    tool_id: str,
    server: str,
    *,
    enabled: bool = True,
    disabled_reason: str | None = None,
) -> ToolSpec:
    """A registry MCP spec as the auto-manifest loader builds it."""
    metadata: dict = {"server": server, "tool": tool_id}
    if disabled_reason is not None:
        metadata["disabled_reason"] = disabled_reason
    return ToolSpec(
        tool_id=tool_id,
        name=tool_id,
        description="",
        factory=lambda: None,  # type: ignore[arg-type, return-value]
        enabled=enabled,
        kind="mcp",
        metadata=metadata,
    )


@pytest.fixture
def _live_env(monkeypatch):
    """Stub the two I/O boundaries: merged MCP config + registry load.

    Two configured servers: ``gitea`` (discovery succeeded — two enabled
    specs) and ``broken`` (discovery failed — one disabled spec carrying the
    manifest's ``disabled_reason``).
    """
    registry = ToolRegistry()
    registry.register(_spec("mcp_gitea_list_issues", "gitea"))
    registry.register(_spec("mcp_gitea_get_file", "gitea"))
    registry.register(
        _spec(
            "mcp_broken_ping",
            "broken",
            enabled=False,
            disabled_reason="Discovery failed: connection refused",
        )
    )
    monkeypatch.setattr(catalog_mod, "load_registry", lambda cwd=None: registry)
    monkeypatch.setattr(
        catalog_mod,
        "get_merged_mcp_config",
        lambda project=None: {"servers": {"gitea": {}, "broken": {}}},
    )
    return registry


def _entry(source_id: str):
    return next(e for e in SourceCatalog.entries() if e.id == source_id)


def test_entries_list_configured_servers_as_live_sources(_live_env) -> None:
    """A configured + discovered MCP server is an available live entry."""
    entry = _entry("gitea")
    assert entry.available is True
    assert entry.source_type == "mcp_tool_list"
    assert entry.tool_ids == ["mcp_gitea_list_issues", "mcp_gitea_get_file"]
    assert entry.unavailable_reason is None


def test_entries_surface_discovery_failure_reason(_live_env) -> None:
    """A configured server whose discovery failed stays listed, greyed out."""
    entry = _entry("broken")
    assert entry.available is False
    assert entry.tool_ids == []
    assert entry.unavailable_reason == "Discovery failed: connection refused"


def test_entries_merge_demo_fixtures_only_while_seeding(_live_env, monkeypatch) -> None:
    """Fixtures append after the live entries with seeding on; vanish when off."""
    ids = [e.id for e in SourceCatalog.entries()]
    assert ids[:2] == ["gitea", "broken"]  # live first, config order
    assert "notion" in ids  # demo fixture merged (seeding defaults on)

    monkeypatch.setattr(catalog_mod, "seeding_enabled", lambda: False)
    ids = [e.id for e in SourceCatalog.entries()]
    assert ids == ["gitea", "broken"]


def test_live_server_id_wins_a_fixture_collision(_live_env, monkeypatch) -> None:
    """A live server named like a fixture id is listed once — the live entry."""
    monkeypatch.setattr(
        catalog_mod,
        "get_merged_mcp_config",
        lambda project=None: {"servers": {"github": {}}},
    )
    entries = [e for e in SourceCatalog.entries() if e.id == "github"]
    assert len(entries) == 1
    assert entries[0].source_type == "mcp_tool_list"
    assert entries[0].desc == "Configured MCP server."


def test_tools_for_grants_live_server_registry_ids(_live_env) -> None:
    """tools_for resolves a live unmapped server to its registry tool ids."""
    assert SourceCatalog.tools_for(["gitea"]) == [
        "mcp_gitea_list_issues",
        "mcp_gitea_get_file",
    ]
    # A discovery-failed server has no enabled specs to grant.
    assert SourceCatalog.tools_for(["broken"]) == []


def test_configured_failed_server_never_falls_back_to_demo_tools(
    _live_env, monkeypatch
) -> None:
    """A configured-but-failed server colliding with a fixture id grants nothing.

    Regression: ``entries`` listed such a server unavailable with no tools while
    ``tools_for`` (sharing the id with a demo fixture, seeding on) granted the
    fixture's demo tools — the two surfaces diverged. Both now resolve through
    ``_source_tool_ids`` with the demo fallback off for configured ids.
    """
    monkeypatch.setattr(
        catalog_mod,
        "get_merged_mcp_config",
        lambda project=None: {"servers": {"github": {}}},  # collides with fixture
    )
    entry = _entry("github")
    assert entry.available is False
    assert entry.tool_ids == []
    assert SourceCatalog.tools_for(["github"]) == []
    # A NON-configured fixture id still resolves its demo tools while seeding on.
    assert SourceCatalog.tools_for(["notion"]) == []  # not in registry → filtered
    assert SourceCatalog._source_tool_ids("notion", live={}) == [
        "notion_search",
        "notion_fetch",
    ]


def test_scg_capability_nodes_override_live_registry_ids(_live_env) -> None:
    """Once a live server is mapped, its SCG capability nodes win resolution."""
    scg_store.get_scg_store().upsert_nodes(
        [
            ScgNode(
                source_key="gitea#mcp_gitea_list_issues",
                kind="capability",
                source_id="gitea",
                name="mcp_gitea_list_issues",
            )
        ]
    )
    assert SourceCatalog.tools_for(["gitea"]) == ["mcp_gitea_list_issues"]
    assert _entry("gitea").tool_ids == ["mcp_gitea_list_issues"]
