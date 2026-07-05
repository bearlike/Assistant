#!/usr/bin/env python3
"""Tests for the orchestration tool cards (issue #161-C).

Each renderer is fed a representative tool-result payload (mirroring the real
``SpawnAgentTool`` / ``check_agents`` / ``tool_search`` envelopes) and we assert
the card surfaces the key fields and is NOT a raw JSON dump.
"""

from __future__ import annotations

import json
from io import StringIO

from mewbo_cli.cli_theme import DEFAULT_PALETTE
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem
from mewbo_cli.tui.transcript_render import register_transcript_renderers
from mewbo_cli.tui.widgets.orchestration_cards import (
    OrchestrationCards,
    register_orchestration_cards,
)
from rich.console import Console


def _render_to_text(rendered: object) -> str:
    """Render a Rich renderable to plain text for assertions."""
    sio = StringIO()
    Console(file=sio, highlight=False, width=200).print(rendered)
    return sio.getvalue()


def _wrapped_registry() -> MessageRendererRegistry:
    """A registry with the base renderers + orchestration cards installed."""
    registry = MessageRendererRegistry()
    register_transcript_renderers(registry, palette=DEFAULT_PALETTE)
    register_orchestration_cards(registry, palette=DEFAULT_PALETTE)
    return registry


# ---------------------------------------------------------------------------
# spawn_agent
# ---------------------------------------------------------------------------


def test_spawn_agent_card_shows_task_model_agent_type() -> None:
    """A single spawn renders task (truncated), model, and agent_type — not JSON."""
    result = json.dumps(
        {
            "agent_id": "abcd1234ef567890",
            "status": "submitted",
            "task": "Investigate the failing retrieval probe and report findings",
            "message": "Agent spawned. Use check_agents to monitor.",
        }
    )
    item = TranscriptItem(
        "tool",
        {
            "tool_id": "spawn_agent",
            "result": result,
            "args_summary": "task=Investigate the failing retrieval probe, "
            "model=sonnet, agent_type=Explore",
        },
    )
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))

    assert "spawn_agent" in text
    assert "Explore" in text  # agent_type
    assert "sonnet" in text  # model
    assert "Investigate the failing retrieval probe" in text  # task
    assert "abcd1234" in text  # short agent id
    # NOT a raw JSON dump.
    assert '"agent_id"' not in text
    assert '"message"' not in text


def test_spawn_agents_batch_card_shows_counts_and_rows() -> None:
    """A batch spawn renders spawned/rejected counts + per-task rows, not JSON."""
    result = json.dumps(
        {
            "kind": "agent_batch",
            "text": "Spawned 2/3 agent(s); 1 rejected.",
            "agents": [
                {"index": 0, "agent_id": "a1", "status": "submitted", "task": "Map the SCG"},
                {"index": 1, "agent_id": "b2", "status": "submitted", "task": "Read configs"},
                {"index": 2, "agent_id": None, "status": "rejected", "task": "Third lane"},
            ],
            "spawned": 2,
            "rejected": 1,
        }
    )
    item = TranscriptItem("tool", {"tool_id": "spawn_agents", "result": result})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))

    assert "spawn_agents" in text
    assert "2 spawned" in text
    assert "1 rejected" in text
    assert "Map the SCG" in text
    assert "Third lane" in text
    assert '"kind"' not in text


def test_spawn_agent_card_degrades_without_result() -> None:
    """A spawn with no parseable result still renders a header (graceful)."""
    item = TranscriptItem("tool", {"tool_id": "spawn_agent", "result": "ERROR: boom"})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))
    assert "spawn_agent" in text


# ---------------------------------------------------------------------------
# check_agents
# ---------------------------------------------------------------------------


def test_check_agents_card_is_a_table_not_json() -> None:
    """check_agents renders a fleet table with status + last tool, not raw JSON."""
    result = json.dumps(
        {
            "kind": "agent_tree",
            "text": "Agent tree:\n...",
            "agents": [
                {
                    "id": "abcd1234 effff",
                    "status": "running",
                    "task": "Probe the index",
                    "steps_completed": 3,
                    "last_tool_id": "scg_search",
                },
                {
                    "id": "9999000011112222",
                    "status": "completed",
                    "task": "Read config",
                    "steps_completed": 5,
                    "last_tool_id": "read",
                },
            ],
            "parent_id": "root",
        }
    )
    item = TranscriptItem("tool", {"tool_id": "check_agents", "result": result})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))

    assert "check_agents" in text
    assert "2 agent(s)" in text
    assert "running" in text
    assert "completed" in text
    assert "scg_search" in text  # last tool column
    assert "abcd1234" in text  # short id
    # Column header for the always-present metric.
    assert "steps" in text
    assert '"agents"' not in text


def test_check_agents_card_uses_tokens_when_present() -> None:
    """The metric column surfaces token totals when the payload carries them."""
    result = json.dumps(
        {
            "agents": [
                {
                    "id": "aaaa1111",
                    "status": "completed",
                    "task": "t",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "last_tool_id": "read",
                }
            ]
        }
    )
    item = TranscriptItem("tool", {"tool_id": "check_agents", "result": result})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))
    assert "tokens" in text  # column header
    assert "150" in text  # input + output


def test_check_agents_card_empty_fleet() -> None:
    """No agents → a one-line 'no agents' card, not a crash."""
    item = TranscriptItem(
        "tool", {"tool_id": "check_agents", "result": json.dumps({"agents": []})}
    )
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))
    assert "check_agents" in text
    assert "no agents" in text


# ---------------------------------------------------------------------------
# tool_search
# ---------------------------------------------------------------------------


def test_tool_search_card_lists_tool_names_and_descriptions() -> None:
    """tool_search renders a name + one-line description list, not the schema dump."""
    raw = (
        "<functions>"
        '<function>{"description": "Read a file from disk.\\nSupports ranges.", '
        '"name": "Read", "parameters": {}}</function>'
        '<function>{"description": "Edit a file in place.", "name": "Edit", '
        '"parameters": {}}</function>'
        "</functions>"
    )
    item = TranscriptItem("tool", {"tool_id": "tool_search", "result": raw})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))

    assert "tool_search" in text
    assert "2 tool(s) loaded" in text
    assert "Read" in text
    assert "Edit" in text
    assert "Read a file from disk." in text  # first line only
    assert "Supports ranges" not in text  # second line dropped
    assert "<function>" not in text  # raw schema not shown
    assert "parameters" not in text


def test_tool_search_card_empty() -> None:
    """A tool_search result with no functions degrades to a one-liner."""
    item = TranscriptItem("tool", {"tool_id": "tool_search", "result": "nothing here"})
    text = _render_to_text(OrchestrationCards(DEFAULT_PALETTE).render(item))
    assert "tool_search" in text
    assert "no tools loaded" in text


# ---------------------------------------------------------------------------
# Registration / dispatch
# ---------------------------------------------------------------------------


def test_non_orchestration_tool_delegates_to_base_renderer() -> None:
    """The wrapping renderer leaves ordinary tools (bash) to the base renderer."""
    registry = _wrapped_registry()
    item = TranscriptItem(
        "tool", {"tool_id": "bash", "operation": "execute", "command": "ls -la", "result": "ok"}
    )
    text = _render_to_text(registry.render(item))
    assert "ls -la" in text  # base bash card still renders the command


def test_orchestration_card_wins_after_registration() -> None:
    """After register_orchestration_cards, spawn_agent routes to the card."""
    registry = _wrapped_registry()
    item = TranscriptItem(
        "tool",
        {"tool_id": "spawn_agent", "result": json.dumps({"status": "submitted", "task": "go"})},
    )
    text = _render_to_text(registry.render(item))
    assert "spawn_agent" in text
    assert "go" in text


def test_render_returns_none_for_non_orchestration_tool() -> None:
    """OrchestrationCards.render returns None for a tool it does not own."""
    cards = OrchestrationCards(DEFAULT_PALETTE)
    assert cards.render(TranscriptItem("tool", {"tool_id": "read"})) is None
    assert cards.render(TranscriptItem("assistant", {"text": "hi"})) is None
