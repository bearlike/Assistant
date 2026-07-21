"""Smoke tests for FastMCP server wiring.

We don't start a live ASGI server; we only assert the server builds and that
the expected tools are registered with the streamable-HTTP path. The tool
*behavior* is covered by ``test_tools.py``.
"""

from __future__ import annotations

import asyncio
import pathlib
from enum import Enum
from typing import Any

import pytest
from mewbo_mcp.config import EffectTier, McpConfig, McpToolPolicy, ToolGroup
from mewbo_mcp.server import build_server

# Tools by GROUP — the subsystem axis.
_SEARCH_TOOLS = {"list_search_workspaces", "search", "get_search_run"}
_STRUCTURED_TOOLS = {"structured_query", "get_structured_run"}
_INTEGRATION_TOOLS = {"list_integrations", "list_projects"}
_TRIGGER_TOOLS = {"list_triggers", "cancel_trigger"}
_SESSION_TOOLS = {
    "create_session",
    "send_followup",
    "interrupt_session",
    "terminate_session",
    # cleanup_worktree is absent by design — worktree lifecycle is system-owned.
    "list_sessions",
    "get_session_history",
    "get_agent_tree",
}
_WIKI_TOOLS = {
    "list_wiki_projects",
    "read_wiki_structure",
    "read_wiki_page",
    "list_wiki_pages",
    "graph_neighbors",
    "submit_insight",
    "ask_wiki",
    "get_wiki_answer",
}

# Groups withheld by DEFAULT — everything but sessions + wiki.
_WITHHELD = _SEARCH_TOOLS | _STRUCTURED_TOOLS | _INTEGRATION_TOOLS | _TRIGGER_TOOLS

# Every tool `build_server` CAN register (the full surface, policy permitting).
_ALL_TOOLS = _SESSION_TOOLS | _WIKI_TOOLS | _WITHHELD

# Tools by EFFECT TIER — the axis that makes "wiki reads but not wiki asks"
# sayable. `submit_insight` is `ask` because the server condenses the raw text
# with a model call; `get_wiki_answer` is `read` because it re-fetches a stored
# snapshot rather than starting a second run.
_WIKI_READ = {
    "list_wiki_projects",
    "read_wiki_structure",
    "read_wiki_page",
    "list_wiki_pages",
    "get_wiki_answer",
}
_WIKI_NAVIGATE = {"graph_neighbors"}
_WIKI_ASK = {"submit_insight", "ask_wiki"}

_cfg = McpConfig(api_url="http://api.test", host="127.0.0.1", port=5125)


def _policy(
    groups: frozenset[ToolGroup] | None = None,
    tiers: frozenset[EffectTier] | None = None,
) -> McpToolPolicy:
    """A policy over the given axes, each defaulting to fully open."""
    return McpToolPolicy(
        exposed=frozenset(ToolGroup) if groups is None else groups,
        tiers=frozenset(EffectTier) if tiers is None else tiers,
    )


def _server(policy: McpToolPolicy):
    return build_server(
        McpConfig(api_url="http://api.test", host="127.0.0.1", port=5125, tools=policy)
    )


def _names(server) -> set[str]:
    return {t.name for t in asyncio.run(server.list_tools())}


def test_only_sessions_and_wiki_are_exposed_by_default():
    """Search, structured, integrations and triggers are all off out of the box."""
    names = _names(build_server(_cfg))
    assert names == _SESSION_TOOLS | _WIKI_TOOLS
    assert not (names & _WITHHELD)


def test_policy_can_expose_every_group():
    """The tools are GATED, not deleted — an operator opts back in explicitly."""
    assert _names(_server(_policy())) == _ALL_TOOLS


def test_each_group_is_independently_gateable():
    """Exposure keys off the group, so one door can open without the other."""
    assert _names(_server(_policy(groups=frozenset({ToolGroup.SEARCH})))) == _SEARCH_TOOLS


def test_wiki_reads_are_exposable_without_wiki_asks():
    """THE case the subsystem axis alone could not express.

    Reading a generated page and asking the wiki a question are the same
    *product* but not the same *cost*: one returns stored bytes, the other starts
    a model run. Withholding the `ask` tier must leave the reads standing.
    """
    policy = _policy(
        groups=frozenset({ToolGroup.WIKI}),
        tiers=frozenset({EffectTier.READ, EffectTier.NAVIGATE}),
    )
    names = _names(_server(policy))
    assert names == _WIKI_READ | _WIKI_NAVIGATE
    assert not (names & _WIKI_ASK)


def test_navigate_is_gateable_independently_of_read():
    """The two zero-cost tiers are still distinct — graph walks can be withheld."""
    policy = _policy(groups=frozenset({ToolGroup.WIKI}), tiers=frozenset({EffectTier.READ}))
    assert _names(_server(policy)) == _WIKI_READ


def test_exposure_is_the_conjunction_of_both_axes():
    """Allowing a tier does not expose a withheld group's tools of that tier."""
    policy = _policy(groups=frozenset({ToolGroup.WIKI}), tiers=frozenset({EffectTier.ASK}))
    names = _names(_server(policy))
    assert names == _WIKI_ASK
    # `search` is also `ask` tier — but its GROUP is withheld, so it stays absent.
    assert "search" not in names


def test_drive_tier_can_be_withheld_leaving_session_reads():
    """An observe-only deployment: read every session, steer none of them."""
    policy = _policy(
        groups=frozenset({ToolGroup.SESSIONS}),
        tiers=frozenset({EffectTier.READ}),
    )
    names = _names(_server(policy))
    assert names == {"list_sessions", "get_session_history", "get_agent_tree"}
    assert "terminate_session" not in names  # irreversible, and `drive` is off


def test_instructions_never_advertise_a_withheld_group():
    """Schema matches behavior (invariant 4) — don't blurb what we withhold."""
    default = build_server(_cfg).instructions or ""
    assert "Mewbo Search" not in default
    assert "structured_query" not in default
    assert "Agentic Wiki" in default  # still exposed

    full = _server(_policy()).instructions or ""
    assert "Mewbo Search" in full
    assert "structured_query" in full


def test_instructions_stop_advertising_asks_when_the_tier_is_withheld():
    """The blurb tracks the EFFECT axis too, not just the group axis."""
    policy = _policy(
        groups=frozenset({ToolGroup.WIKI}),
        tiers=frozenset({EffectTier.READ, EffectTier.NAVIGATE}),
    )
    text = _server(policy).instructions or ""
    assert "Read the Agentic Wiki's generated pages" in text
    assert "Walk a project's code graph" in text
    assert "Ask the Agentic Wiki" not in text
    # No `ask`-tier tool survives, so there is nothing long-running to promise.
    assert "Long-running tools" not in text


def test_default_exposure_decides_every_group():
    """A new ToolGroup can't silently auto-expose — the map must decide it."""
    from mewbo_mcp.config import _DEFAULT_EXPOSURE

    assert set(_DEFAULT_EXPOSURE) == set(ToolGroup)


def test_default_exposure_decides_every_effect_tier():
    """Same guarantee on the new axis: a new EffectTier must be decided too."""
    from mewbo_mcp.config import _DEFAULT_TIER_EXPOSURE

    assert set(_DEFAULT_TIER_EXPOSURE) == set(EffectTier)


def test_an_undecided_axis_member_raises_at_import():
    """The exhaustiveness guard is a hard error, not a warning — prove it fires.

    Re-runs the module's own guard over a map that is deliberately missing a
    member, so the assertion covers the real loop rather than a restatement of it.
    """
    import mewbo_mcp.config as config_mod

    source = pathlib.Path(config_mod.__file__).read_text()
    guard = source[source.index("_EXPOSURE_AXES:") : source.index("@dataclass")]
    incomplete = dict(config_mod._DEFAULT_TIER_EXPOSURE)
    incomplete.pop(EffectTier.NAVIGATE)
    namespace = {
        "_DEFAULT_EXPOSURE": config_mod._DEFAULT_EXPOSURE,
        "_DEFAULT_TIER_EXPOSURE": incomplete,
        "ToolGroup": ToolGroup,
        "EffectTier": EffectTier,
        "Any": Any,
        "Enum": Enum,
    }
    with pytest.raises(RuntimeError, match="must decide every EffectTier"):
        exec(guard, namespace)  # noqa: S102 — running the module's own guard


def test_policy_from_env_is_a_fail_closed_allowlist(monkeypatch):
    monkeypatch.delenv(McpToolPolicy.ENV_VAR, raising=False)
    monkeypatch.delenv(McpToolPolicy.TIER_ENV_VAR, raising=False)
    assert McpToolPolicy.from_env().exposed == {ToolGroup.SESSIONS, ToolGroup.WIKI}
    # Naming a group is the ONLY way to expose it.
    monkeypatch.setenv(McpToolPolicy.ENV_VAR, "sessions, search")
    assert McpToolPolicy.from_env().exposed == {ToolGroup.SESSIONS, ToolGroup.SEARCH}
    monkeypatch.setenv(McpToolPolicy.ENV_VAR, "")
    assert McpToolPolicy.from_env().exposed == frozenset()


def test_tier_env_var_is_its_own_fail_closed_allowlist(monkeypatch):
    """The axes resolve independently — narrowing one must not touch the other."""
    monkeypatch.delenv(McpToolPolicy.ENV_VAR, raising=False)
    monkeypatch.delenv(McpToolPolicy.TIER_ENV_VAR, raising=False)
    assert McpToolPolicy.from_env().tiers == set(EffectTier)

    monkeypatch.setenv(McpToolPolicy.TIER_ENV_VAR, "read, navigate")
    policy = McpToolPolicy.from_env()
    assert policy.tiers == {EffectTier.READ, EffectTier.NAVIGATE}
    assert policy.exposed == {ToolGroup.SESSIONS, ToolGroup.WIKI}  # group axis untouched
    assert policy.allows(ToolGroup.WIKI, EffectTier.READ)
    assert not policy.allows(ToolGroup.WIKI, EffectTier.ASK)

    monkeypatch.setenv(McpToolPolicy.TIER_ENV_VAR, "")
    assert McpToolPolicy.from_env().tiers == frozenset()


def test_unknown_group_fails_fast(monkeypatch):
    """A typo must never silently withhold (or expose) a group."""
    monkeypatch.setenv(McpToolPolicy.ENV_VAR, "sessions, serach")
    with pytest.raises(ValueError, match="unknown ToolGroup member"):
        McpToolPolicy.from_env()


def test_unknown_tier_fails_fast(monkeypatch):
    """Same rule on the effect axis — an unknown tier is a hard startup error."""
    monkeypatch.setenv(McpToolPolicy.TIER_ENV_VAR, "read, navgate")
    with pytest.raises(ValueError, match="unknown EffectTier member"):
        McpToolPolicy.from_env()


def test_streamable_http_path_is_mcp():
    server = build_server(McpConfig(api_url="http://api.test", host="0.0.0.0", port=9000))
    assert server.settings.streamable_http_path == "/mcp"
    assert server.settings.host == "0.0.0.0"
    assert server.settings.port == 9000


def test_config_from_env_defaults(monkeypatch):
    monkeypatch.delenv("MEWBO_API_URL", raising=False)
    monkeypatch.delenv("MEWBO_MCP_HOST", raising=False)
    monkeypatch.delenv("MEWBO_MCP_PORT", raising=False)
    cfg = McpConfig.from_env()
    assert cfg.api_url == "http://localhost:5124"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 5127


def test_config_from_env_overrides(monkeypatch):
    monkeypatch.setenv("MEWBO_API_URL", "https://mewbo.example.com/")
    monkeypatch.setenv("MEWBO_MCP_PORT", "7000")
    cfg = McpConfig.from_env()
    assert cfg.api_url == "https://mewbo.example.com"  # trailing slash stripped
    assert cfg.port == 7000
