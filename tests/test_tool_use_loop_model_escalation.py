#!/usr/bin/env python3
"""#54 fallback ladder × #113 per-model variants: escalation re-variants prompts.

When the resilience strategy escalates to (and pins) a fallback model, the loop
must re-render its system prompt AND re-derive its edit-tool variant against the
ESCALATED model — so the heal is behavioural, not just a model swap. These tests
drive ``ToolUseLoop._apply_model_escalation`` directly (the seam the run loop
calls each turn) with the real prompt registry + model-variant map.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from langchain_core.messages import SystemMessage
from mewbo_core.common import get_system_prompt
from mewbo_core.tool_registry import ToolSpec
from mewbo_core.tool_use_loop import ToolUseLoop
from test_tool_use_loop import (
    _allow_all_policy,
    _make_agent_context,
    _make_hook_manager,
    _make_registry,
)

_PATCH_NUDGE = "Structured-patch discipline"


def _file_edit_spec() -> ToolSpec:
    """A local spec whose prompt_path maps to the registry's file.tools.file-edit."""
    return ToolSpec(
        tool_id="file_edit_tool",
        name="File Edit",
        description="edit",
        factory=lambda: None,
        kind="local",
        prompt_path="tools/file-edit",
        metadata={"schema": {"type": "object", "properties": {}}},
    )

# The gemma append override (loop.depth.root) ships this exact marker; the
# structured-patch family (gpt-*) prefers file_edit_tool per model_variants.yaml.
_GEMMA_MARKER = "# Compatibility nudge (gemma)"


def _loop(model_name: str) -> ToolUseLoop:
    loop = ToolUseLoop(
        agent_context=_make_agent_context(model_name=model_name),
        tool_registry=_make_registry(),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )
    # Minimal run()-set state the escalation/select path reads.
    loop._tool_specs_full = []
    loop._tool_search_enabled = False
    loop._deferred_ids = set()
    loop._last_active_ids = set()
    return loop


def test_escalation_rerenders_prompt_and_edit_variant():
    # Primary prefers structured_patch (gpt-4o); escalate to a gemma model that
    # has BOTH a per-model prompt override and the other edit-tool variant.
    loop = _loop("openai/gpt-4o")
    assert loop._active_model == "openai/gpt-4o"
    assert loop._configured_edit_tool_id() == "file_edit_tool"  # structured_patch

    base_system = loop._render_system_prompt(None, None, "")
    assert _GEMMA_MARKER not in base_system  # base = no gemma nudge
    messages = [SystemMessage(content=base_system)]

    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value.bind_tools.return_value = MagicMock()
        tool_schemas, model = loop._apply_model_escalation(
            "gemma-2-9b",
            messages,
            context=None,
            plan=None,
            agent_tree="",
            tool_schemas=[],
            model=MagicMock(),
        )

    # Active model promoted; prompt re-rendered WITH the escalated override.
    assert loop._active_model == "gemma-2-9b"
    assert _GEMMA_MARKER in messages[0].content
    # The base contract is preserved (append, not replace) — convergence.
    assert "Default: Direct execution" in messages[0].content
    # Edit-tool VARIANT adapted in lockstep (gemma → search/replace blocks).
    assert loop._configured_edit_tool_id() == "aider_edit_block_tool"
    # The escalated model was rebound for the next turn.
    mock_build.assert_called_once_with(model_name="gemma-2-9b")


def test_no_escalation_is_a_noop():
    loop = _loop("openai/gpt-4o")
    messages = [SystemMessage(content="ORIGINAL")]
    sentinel_schemas: list = []
    sentinel_model = object()

    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        tool_schemas, model = loop._apply_model_escalation(
            "openai/gpt-4o",  # same model — no switch
            messages,
            context=None,
            plan=None,
            agent_tree="",
            tool_schemas=sentinel_schemas,
            model=sentinel_model,
        )

    assert loop._active_model == "openai/gpt-4o"
    assert messages[0].content == "ORIGINAL"  # untouched
    assert tool_schemas is sentinel_schemas and model is sentinel_model
    mock_build.assert_not_called()  # no rebind on a no-op


def test_tool_guidance_applies_per_model_override_in_production_path():
    # Drives the REAL _render_tool_guidance render site (not a direct
    # registry.render): a gpt- model must get the structured-patch nudge that
    # pairs with its edit-tool variant; a non-matching model must not, and must
    # equal the legacy raw-file guidance byte-for-byte.
    # Production model ids are bare (the proxy prefix is added only at the
    # LiteLLM call seam), which is the form the prompt registry matches against.
    loop = ToolUseLoop(
        agent_context=_make_agent_context(model_name="gpt-5"),
        tool_registry=_make_registry(_file_edit_spec()),
        permission_policy=_allow_all_policy(),
        hook_manager=_make_hook_manager(),
    )
    assert _PATCH_NUDGE in loop._render_tool_guidance()

    loop._active_model = "claude-opus-4-8"
    guidance = loop._render_tool_guidance()
    assert _PATCH_NUDGE not in guidance
    # Base render is byte-identical to the legacy path (no override matched).
    assert guidance == get_system_prompt("tools/file-edit")


def test_empty_final_model_is_a_noop():
    loop = _loop("openai/gpt-4o")
    messages = [SystemMessage(content="ORIGINAL")]
    tool_schemas, model = loop._apply_model_escalation(
        "",
        messages,
        context=None,
        plan=None,
        agent_tree="",
        tool_schemas=[],
        model=None,
    )
    assert loop._active_model == "openai/gpt-4o"
    assert messages[0].content == "ORIGINAL"
