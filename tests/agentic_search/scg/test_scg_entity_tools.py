"""SCG search can read the shared abstract-entity graph via resolve_entity."""
from __future__ import annotations

import json
from pathlib import Path

from mewbo_core.agent_registry import parse_agent_def

SCG_PLUGIN = Path("packages/mewbo_graph/src/mewbo_graph/plugins/scg")
SCG_AGENTS = SCG_PLUGIN / "agents"


def test_scg_search_has_resolve_entity():
    ad = parse_agent_def(SCG_AGENTS / "scg-search.md", source="plugin:scg")
    assert ad is not None
    assert "resolve_entity" in set(ad.allowed_tools or [])
    assert "resolve_entity" in ad.body


def test_scg_manifest_registers_entity_tools():
    manifest = json.loads((SCG_PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    tool_ids = {t.get("tool_id", "") for t in manifest.get("session_tools", [])}
    assert {"mint_entity", "relate_entities", "resolve_entity"}.issubset(tool_ids)
