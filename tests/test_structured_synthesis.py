"""Tests for ``mewbo_core.structured_synthesis.StructuredSynthesizer``.

Design:
- Fake ``GroundingProvider`` returns 2 Citations.
- Stubbed ``build_chat_model`` whose ``.bind_tools(...).ainvoke(...)`` returns an
  ``AIMessage`` with a valid ``emit_result`` tool call.
- Asserts: ``synthesize`` returns the validated payload + the citations.
- Asserts: a schema-invalid first response then a valid reask → returns payload
  after ONE reask.
- Asserts: NO ``Orchestrator``/``ToolUseLoop`` is invoked (the model is called
  once or twice, directly — ``ainvoke`` call count is asserted).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage
from mewbo_core.structured_response import StructuredResponseError
from mewbo_core.structured_synthesis import Citation, StructuredSynthesizer

# ---------------------------------------------------------------------------
# Schema used across tests
# ---------------------------------------------------------------------------

_PERSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer"},
    },
    "required": ["name"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit_response(args: dict, call_id: str = "call_1") -> AIMessage:
    """Fake AIMessage that carries an emit_result tool call."""
    return AIMessage(
        content="",
        tool_calls=[{"name": "emit_result", "args": args, "id": call_id}],
    )


def _text_response(content: str = "prose answer") -> AIMessage:
    """Fake AIMessage with no tool call (model answered in prose)."""
    return AIMessage(content=content, tool_calls=[])


class _FakeGroundingProvider:
    """Implements GroundingProvider — returns 2 deterministic Citations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def search(self, slug: str, query: str, *, k: int = 8) -> list[Citation]:
        self.calls.append((slug, query, k))
        return [
            Citation(id="p1", kind="page", snippet="Auth flow", score=0.9, source="auth.md"),
            Citation(
                id="n1",
                kind="node",
                snippet="def authenticate():",
                score=0.8,
                source="auth.py#authenticate",
            ),
        ]


def _stub_model(responses: list[AIMessage]):
    """Return a stubbed model whose bind_tools().ainvoke() pops from *responses*."""
    bound = MagicMock()
    bound.ainvoke = AsyncMock(side_effect=responses)

    model = MagicMock()
    model.bind_tools.return_value = bound
    return model, bound


# ---------------------------------------------------------------------------
# Test 1: happy path — grounding + single valid emit_result
# ---------------------------------------------------------------------------


def test_synthesize_returns_payload_and_citations():
    """synthesize() returns (validated_payload, citations) in the happy path.

    Asserts:
    - GroundingProvider.search is called with the correct slug and query.
    - The returned payload matches the emit_result args.
    - Citations are the 2 fake ones from the provider.
    - build_chat_model / ainvoke is called ONCE (no Orchestrator, no ToolUseLoop).
    """
    grounding = _FakeGroundingProvider()
    synthesizer = StructuredSynthesizer(
        model_name="test-model",
        grounding_provider=grounding,
    )
    responses = [_emit_response({"name": "Ada", "age": 36})]
    model, bound = _stub_model(responses)

    with patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model):
        payload, citations = asyncio.run(
            synthesizer.synthesize(
                "Who is Ada?",
                _PERSON_SCHEMA,
                workspace="org/repo",
            )
        )

    # Payload: validated dict
    assert payload == {"name": "Ada", "age": 36}, f"Unexpected payload: {payload!r}"

    # Citations: the 2 fake ones
    assert len(citations) == 2
    assert citations[0].id == "p1"
    assert citations[1].id == "n1"

    # GroundingProvider was called
    assert len(grounding.calls) == 1
    slug_called, query_called, _ = grounding.calls[0]
    assert slug_called == "org/repo"
    assert "Ada" in query_called

    # ainvoke called exactly ONCE — no loop/orchestrator
    assert bound.ainvoke.call_count == 1, (
        f"Expected 1 ainvoke call (direct round-trip), got {bound.ainvoke.call_count}. "
        "No ToolUseLoop/Orchestrator must be invoked."
    )


# ---------------------------------------------------------------------------
# Test 2: no workspace → no grounding calls, empty citations
# ---------------------------------------------------------------------------


def test_synthesize_without_workspace_skips_grounding():
    """Without a workspace, grounding is skipped and citations is empty."""
    grounding = _FakeGroundingProvider()
    synthesizer = StructuredSynthesizer(
        model_name="test-model",
        grounding_provider=grounding,
    )
    responses = [_emit_response({"name": "Grace"})]
    model, bound = _stub_model(responses)

    with patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model):
        payload, citations = asyncio.run(
            synthesizer.synthesize("Who is Grace?", _PERSON_SCHEMA)
        )

    assert payload == {"name": "Grace"}
    assert citations == [], "No workspace → no citations expected."
    assert len(grounding.calls) == 0, "Grounding must NOT be called without a workspace."
    assert bound.ainvoke.call_count == 1


# ---------------------------------------------------------------------------
# Test 3: schema-invalid first response → ONE reask → valid second response
# ---------------------------------------------------------------------------


def test_synthesize_reask_on_validation_failure():
    """On schema-invalid first response, ONE reask is issued and payload is returned.

    Sequence:
      Turn 1: emit_result({age: 99}) — missing required 'name' → validation failure.
      Turn 2 (reask): emit_result({name: 'Turing', age: 99}) — valid.

    Asserts:
    - Payload is the turn-2 value.
    - ainvoke called EXACTLY twice (direct reask, NO loop).
    """
    synthesizer = StructuredSynthesizer(model_name="test-model")
    responses = [
        _emit_response({"age": 99}, call_id="c1"),          # invalid — missing 'name'
        _emit_response({"name": "Turing", "age": 99}, call_id="c2"),  # valid
    ]
    model, bound = _stub_model(responses)

    with patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model):
        payload, citations = asyncio.run(
            synthesizer.synthesize("Who is Turing?", _PERSON_SCHEMA)
        )

    assert payload == {"name": "Turing", "age": 99}, f"Unexpected payload: {payload!r}"
    assert citations == []
    assert bound.ainvoke.call_count == 2, (
        f"Expected exactly 2 ainvoke calls (original + reask), got {bound.ainvoke.call_count}. "
        "The reask must be a direct second ainvoke, NOT a ToolUseLoop."
    )


# ---------------------------------------------------------------------------
# Test 4: both responses invalid → StructuredResponseError raised
# ---------------------------------------------------------------------------


def test_synthesize_raises_on_repeated_invalid():
    """After max_failures validation failures, StructuredResponseError is raised."""
    synthesizer = StructuredSynthesizer(model_name="test-model", max_failures=2)
    responses = [
        _emit_response({"age": 1}, call_id="c1"),   # invalid — no 'name'
        _emit_response({"age": 2}, call_id="c2"),   # still invalid
    ]
    model, _ = _stub_model(responses)

    with patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model):
        with pytest.raises(StructuredResponseError):
            asyncio.run(synthesizer.synthesize("Query", _PERSON_SCHEMA))


# ---------------------------------------------------------------------------
# Test 5: model returns no tool call → reask → valid
# ---------------------------------------------------------------------------


def test_synthesize_reask_when_model_returns_prose():
    """When the model returns prose (no tool call), ONE reask issues then emits."""
    synthesizer = StructuredSynthesizer(model_name="test-model")
    responses = [
        _text_response("The answer is Ada."),              # prose, no tool call
        _emit_response({"name": "Ada"}, call_id="c2"),     # valid on reask
    ]
    model, bound = _stub_model(responses)

    with patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model):
        payload, _ = asyncio.run(synthesizer.synthesize("Who?", _PERSON_SCHEMA))

    assert payload == {"name": "Ada"}
    assert bound.ainvoke.call_count == 2


# ---------------------------------------------------------------------------
# Test 6: no ToolUseLoop / Orchestrator imported or instantiated
# ---------------------------------------------------------------------------


def test_no_tool_use_loop_or_orchestrator_instantiated():
    """Confirm ToolUseLoop and Orchestrator are never instantiated by StructuredSynthesizer."""
    synthesizer = StructuredSynthesizer(model_name="test-model")
    responses = [_emit_response({"name": "Test"})]
    model, _ = _stub_model(responses)

    with (
        patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model),
        patch("mewbo_core.tool_use_loop.ToolUseLoop") as mock_loop,
        patch("mewbo_core.orchestrator.Orchestrator") as mock_orch,
    ):
        asyncio.run(synthesizer.synthesize("Test", _PERSON_SCHEMA))

    assert mock_loop.call_count == 0, (
        "ToolUseLoop must NOT be instantiated by StructuredSynthesizer."
    )
    assert mock_orch.call_count == 0, (
        "Orchestrator must NOT be instantiated by StructuredSynthesizer."
    )


# ---------------------------------------------------------------------------
# Test 7: GroundingProvider Protocol compliance
# ---------------------------------------------------------------------------


def test_grounding_provider_protocol_compliance():
    """WikiGroundingProvider (and fake) satisfy the GroundingProvider Protocol."""
    fake = _FakeGroundingProvider()
    # GroundingProvider is structural (not runtime_checkable) — duck-type check.
    assert callable(getattr(fake, "search", None)), "Provider must have a callable search method."
    # Verify the return type: list of Citation
    result = fake.search("slug", "query", k=3)
    assert isinstance(result, list)
    assert all(isinstance(c, Citation) for c in result)


# ---------------------------------------------------------------------------
# Test 8: Citation dataclass is frozen / hashable
# ---------------------------------------------------------------------------


def test_citation_is_frozen():
    """Citation is a frozen dataclass (immutable)."""
    c = Citation(id="x", kind="page", snippet="s", score=0.5, source="src")
    with pytest.raises((AttributeError, TypeError)):
        c.id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Test 9: Langfuse callback is attached to the model invoke (#87)
# ---------------------------------------------------------------------------


def test_synthesize_attaches_langfuse_callback_to_invoke():
    """The synthesis ``ainvoke`` carries the Langfuse ``config`` so it EXPORTS.

    ``langfuse_session_context`` only PROPAGATES attributes; with no observation
    created inside, nothing reaches Langfuse (the #87 defect: realtime synthesis
    traced nothing). The fix attaches the ``CallbackHandler`` at the invoke seam.
    We stub ``langfuse_invoke_config`` (the import-guarded helper) with a sentinel
    handler and assert the model invoke received it via ``config``.
    """
    sentinel_handler = object()
    fake_config = {"callbacks": [sentinel_handler], "metadata": {"k": "v"}}

    synthesizer = StructuredSynthesizer(model_name="test-model")
    responses = [_emit_response({"name": "Ada"})]
    model, bound = _stub_model(responses)

    with (
        patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model),
        patch(
            "mewbo_core.structured_synthesis.langfuse_invoke_config",
            return_value=fake_config,
        ),
    ):
        asyncio.run(synthesizer.synthesize("Who is Ada?", _PERSON_SCHEMA))

    # The invoke received the langfuse config (callbacks attached → trace exports).
    assert bound.ainvoke.await_count == 1
    _args, kwargs = bound.ainvoke.call_args
    assert kwargs.get("config") is fake_config, (
        "synthesize() must pass the langfuse invoke config (with the CallbackHandler) "
        f"to ainvoke; got config={kwargs.get('config')!r}"
    )


def test_synthesize_disabled_langfuse_passes_no_config():
    """When Langfuse is off (``{}``), the invoke config degrades to ``None`` (no-op)."""
    synthesizer = StructuredSynthesizer(model_name="test-model")
    responses = [_emit_response({"name": "Ada"})]
    model, bound = _stub_model(responses)

    with (
        patch("mewbo_core.structured_synthesis.build_chat_model", return_value=model),
        patch("mewbo_core.structured_synthesis.langfuse_invoke_config", return_value={}),
    ):
        asyncio.run(synthesizer.synthesize("Who is Ada?", _PERSON_SCHEMA))

    _args, kwargs = bound.ainvoke.call_args
    assert kwargs.get("config") is None, "empty config must pass through as None"
