"""tests/wiki/test_agent_defs.py"""
from pathlib import Path

from mewbo_core.agent_registry import parse_agent_def

WIKI_AGENTS_DIR = Path(
    "packages/mewbo_graph/src/mewbo_graph/plugins/wiki/agents"
)


def test_wiki_indexer_agent_def_loads():
    path = WIKI_AGENTS_DIR / "wiki-indexer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-indexer"
    expected_tools = {
        "wiki_clone_repo", "wiki_scan_tree", "wiki_load_grounder",
        "wiki_commit_plan", "wiki_finalize", "wiki_submit_insight",
        "spawn_agent", "check_agents", "read_file", "glob", "grep", "ls",
    }
    actual_tools = set(agent_def.allowed_tools or [])
    assert expected_tools.issubset(actual_tools), \
        f"Missing tools: {expected_tools - actual_tools}"


def test_wiki_indexer_agent_body_contains_milestones():
    path = WIKI_AGENTS_DIR / "wiki-indexer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    body = agent_def.body
    for keyword in ["wiki_clone_repo", "wiki_load_grounder", "wiki_scan_tree",
                    "wiki_commit_plan", "spawn_agent", "wiki_finalize",
                    ".mewbo/wiki.json", ".devin/wiki.json"]:
        assert keyword in body, f"Indexer playbook missing reference to {keyword!r}"


def test_wiki_page_writer_agent_def_loads():
    path = WIKI_AGENTS_DIR / "wiki-page-writer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-page-writer"
    expected_tools = {"read_file", "glob", "grep", "wiki_submit_page"}
    actual_tools = set(agent_def.allowed_tools or [])
    assert expected_tools.issubset(actual_tools)
    assert "wiki_submit_page" in agent_def.body


def test_wiki_qa_agent_def_loads():
    path = WIKI_AGENTS_DIR / "wiki-qa.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-qa"
    tools = set(agent_def.allowed_tools or [])
    # Root is a hypervisor: it orients, dispatches probes, and emits the answer.
    expected_tools = {"wiki_list_pages", "spawn_agent", "check_agents",
                      "wiki_emit_block", "wiki_submit_insight"}
    assert expected_tools.issubset(tools)
    # It must NOT carry the probe's retrieval tools — delegation is the point.
    assert "wiki_query_graph" not in tools
    assert "wiki_code_search" not in tools
    for kw in ["spawn_agent", "wiki-qa-probe", "wiki_emit_block", "probe"]:
        assert kw in agent_def.body


def test_wiki_qa_probe_agent_def_loads():
    """wiki-qa-probe is the leaf: full read-only retrieval surface, no emit/spawn."""
    path = WIKI_AGENTS_DIR / "wiki-qa-probe.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-qa-probe"
    assert agent_def.requires_capabilities == ("wiki",)
    tools = set(agent_def.allowed_tools or [])
    expected_tools = {"wiki_query_graph", "wiki_graph_neighbors", "wiki_code_search",
                      "wiki_search_pages", "wiki_read_page", "wiki_read_file",
                      "wiki_grep", "wiki_list_files", "wiki_submit_insight"}
    assert expected_tools.issubset(tools)
    # A probe never writes the answer and never fans out further.
    assert "wiki_emit_block" not in tools
    assert "spawn_agent" not in tools
    assert "wiki_emit_block" in set(agent_def.denied_tools or [])
    assert "spawn_agent" in set(agent_def.denied_tools or [])
    # Body teaches the ANN-probe instinct + the return contract.
    for kw in ["graph", "FINDINGS", "CITE", "seed"]:
        assert kw in agent_def.body


def test_plugin_manifest_lists_all_agents():
    """plugin.json declares every wiki agent, including the QA probe leaf."""
    import json
    manifest = json.loads(
        (Path("packages/mewbo_graph/src/mewbo_graph/plugins/wiki/.claude-plugin/plugin.json")).read_text()
    )
    agent_paths = {entry.get("path", "") for entry in manifest.get("agents", [])}
    assert "agents/wiki-indexer.md" in agent_paths
    assert "agents/wiki-page-writer.md" in agent_paths
    assert "agents/wiki-qa.md" in agent_paths
    assert "agents/wiki-qa-probe.md" in agent_paths


def test_wiki_enricher_agent_def_loads():
    path = WIKI_AGENTS_DIR / "wiki-enricher.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-enricher"
    expected_tools = {"read_file", "grep", "wiki_query_graph", "mint_entity",
                      "relate_entities", "resolve_entity"}
    assert expected_tools.issubset(set(agent_def.allowed_tools or []))
    for kw in ["mint_entity", "relate_entities", "AST", "source prose"]:
        assert kw in agent_def.body


def test_wiki_enricher_registered_in_manifest():
    import json
    manifest = json.loads(
        (Path("packages/mewbo_graph/src/mewbo_graph/plugins/wiki/.claude-plugin/plugin.json")).read_text()
    )
    agent_paths = {entry.get("path", "") for entry in manifest.get("agents", [])}
    assert "agents/wiki-enricher.md" in agent_paths


def test_indexer_carries_entity_tools_and_enrich_step():
    path = WIKI_AGENTS_DIR / "wiki-indexer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    tools = set(agent_def.allowed_tools or [])
    assert {"mint_entity", "relate_entities", "resolve_entity"}.issubset(tools)
    assert "wiki-enricher" in agent_def.body
    assert "enrich" in agent_def.body.lower()


def test_page_writer_can_resolve_entities_and_submit_insight():
    path = WIKI_AGENTS_DIR / "wiki-page-writer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    tools = set(agent_def.allowed_tools or [])
    assert {"resolve_entity", "wiki_submit_insight"}.issubset(tools)
    assert "resolve_entity" in agent_def.body


def test_indexer_page_writer_spawn_grants_entity_tools():
    """The page-writer spawn list must be a superset of the entity tools the
    page-writer AgentDef declares + uses; ``filter_specs`` silently drops any
    id not in the spawn allowlist, so a missing id is unreachable at runtime.
    """
    import re

    body = (WIKI_AGENTS_DIR / "wiki-indexer.md").read_text()
    # The page-writer spawn block names allowed_tools=[...]; pull the LAST one
    # (the page-writer fan-out spawn) and parse its id list.
    matches = re.findall(r"allowed_tools=\[([^\]]*)\]", body)
    assert matches, "indexer playbook declares no spawn allowed_tools"
    spawn_tools = {t.strip().strip('"').strip("'") for t in matches[-1].split(",")}
    assert {"resolve_entity", "wiki_submit_insight", "wiki_submit_page"}.issubset(
        spawn_tools
    ), f"page-writer spawn missing entity tools: {spawn_tools}"
