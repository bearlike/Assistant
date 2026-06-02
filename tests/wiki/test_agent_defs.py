"""tests/wiki/test_agent_defs.py"""
from pathlib import Path

from mewbo_core.agent_registry import parse_agent_def

WIKI_AGENTS_DIR = Path(
    "packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/agents"
)


def test_wiki_indexer_agent_def_loads():
    path = WIKI_AGENTS_DIR / "wiki-indexer.md"
    agent_def = parse_agent_def(path, source="plugin:wiki")
    assert agent_def is not None
    assert agent_def.name == "wiki-indexer"
    expected_tools = {
        "wiki_clone_repo", "wiki_scan_tree", "wiki_load_grounder",
        "wiki_commit_plan", "wiki_finalize", "spawn_agent",
        "check_agents", "read_file", "glob", "grep", "ls",
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
    expected_tools = {"wiki_search_pages", "wiki_read_page", "wiki_code_search",
                      "wiki_query_graph", "wiki_emit_block"}
    assert expected_tools.issubset(set(agent_def.allowed_tools or []))
    for kw in ["wiki_emit_block", "wiki_search_pages", "wiki_read_page"]:
        assert kw in agent_def.body


def test_plugin_manifest_lists_both_agents():
    """plugin.json declares all wiki agents."""
    import json
    manifest = json.loads(
        (Path("packages/mewbo_core/src/mewbo_core/builtin_plugins/wiki/.claude-plugin/plugin.json")).read_text()
    )
    agent_paths = {entry.get("path", "") for entry in manifest.get("agents", [])}
    assert "agents/wiki-indexer.md" in agent_paths
    assert "agents/wiki-page-writer.md" in agent_paths
    assert "agents/wiki-qa.md" in agent_paths
