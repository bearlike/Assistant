"""Tests for the schema-constrained structured-response emit tool + responder."""
from __future__ import annotations

import asyncio
import threading

from mewbo_core.classes import ActionStep
from mewbo_core.exit_plan_mode import ExitPlanModeTool
from mewbo_core.permissions import auto_approve
from mewbo_core.session_provenance import SessionOrigin
from mewbo_core.structured_response import (
    FORCE_EMIT_DIRECTIVE,
    EmitStructuredResponseTool,
    StructuredResponder,
    StructuredResponseError,
)

_PERSON_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
    "additionalProperties": False,
}


def _step(tool_input: dict) -> ActionStep:
    return ActionStep(
        title="emit_result", tool_id="emit_result", operation="set", tool_input=tool_input
    )


def test_emit_tool_schema_wraps_caller_schema():
    tool = EmitStructuredResponseTool(session_id="s1", schema=_PERSON_SCHEMA)
    assert tool.tool_id == "emit_result"
    assert tool.schema["type"] == "function"
    fn = tool.schema["function"]
    assert fn["name"] == "emit_result"
    # The caller schema is bound verbatim as the tool's parameters.
    assert fn["parameters"]["properties"]["name"] == {"type": "string"}
    assert fn["parameters"]["required"] == ["name"]


def test_emit_tool_valid_payload_emits_event_and_terminates():
    events: list[dict] = []
    tool = EmitStructuredResponseTool(
        session_id="s1", schema=_PERSON_SCHEMA, event_logger=events.append
    )
    result = asyncio.run(tool.handle(_step({"name": "Ada", "age": 36})))
    assert "Structured output accepted" in result.content
    assert tool.should_terminate_run() is True
    # Flag consumed — second poll is False.
    assert tool.should_terminate_run() is False
    assert tool.payload == {"name": "Ada", "age": 36}
    assert len(events) == 1
    assert events[0]["type"] == "structured_output"
    assert events[0]["payload"] == {"name": "Ada", "age": 36}


def test_emit_tool_invalid_payload_reasks_then_gives_up():
    events: list[dict] = []
    tool = EmitStructuredResponseTool(
        session_id="s1", schema=_PERSON_SCHEMA, event_logger=events.append, max_failures=2
    )
    # Missing required "name" — first call reasks, does not terminate.
    r1 = asyncio.run(tool.handle(_step({"age": 36})))
    assert "did not match the schema" in r1.content
    assert "name" in r1.content
    assert tool.should_terminate_run() is False
    assert tool.payload is None
    assert events == []
    # Second invalid call hits the cap → failure marker + terminate.
    r2 = asyncio.run(tool.handle(_step({"age": 36})))
    assert "giving up" in r2.content
    assert tool.failed is True
    assert tool.should_terminate_run() is True
    assert events[-1]["payload"]["_error"] == "schema_validation_failed"


def test_emit_tool_non_object_root_is_wrapped_in_result():
    tool = EmitStructuredResponseTool(
        session_id="s1", schema={"type": "array", "items": {"type": "string"}}
    )
    params = tool.schema["function"]["parameters"]
    assert params["type"] == "object"
    assert params["properties"]["result"]["type"] == "array"
    # And the wrapped payload validates against the wrapping object.
    result = asyncio.run(tool.handle(_step({"result": ["a", "b"]})))
    assert "accepted" in result.content


class _FakeRuntime:
    """Minimal SessionRuntime double that drives the REAL emit path.

    ``run_sync`` replays one or more ``tool_input`` payloads through the actual
    ``emit.handle()`` the responder injected — so ``emit.payload`` / ``emit.failed``
    are set exactly as production would, exercising the code under test end-to-end.
    Passing ``None`` for ``tool_inputs`` simulates a run where the model never
    called ``emit_result`` (the tool's ``handle`` is never invoked).
    """

    def __init__(self, tool_inputs, *, redrive_tool_inputs=None):
        self._tool_inputs = tool_inputs
        # When set, the SECOND run_sync (the belt-and-suspenders re-drive)
        # replays these payloads instead — simulating a model that only emits
        # once forced by the re-drive directive.
        self._redrive_tool_inputs = redrive_tool_inputs
        self.context_events: list[dict] = []
        self.events: list[dict] = []
        self.tags: list[str] = []
        self.run_kwargs: dict = {}
        self.run_calls: list[dict] = []

    def resolve_session(self, *, session_tag=None, session_id=None):
        return "sess-1"

    def tag_session(self, session_id, tag):
        self.tags.append(tag)

    def append_context_event(self, session_id, context):
        self.context_events.append(context)

    def append_event(self, session_id, event):
        # Mirrors SessionRuntime.append_event: records the raw transcript event
        # verbatim so the terminal-failure completion is observable in tests (#40).
        self.events.append(event)

    def run_sync(self, **kwargs):
        self.run_kwargs = kwargs
        self.run_calls.append(kwargs)
        emit = kwargs["extra_session_tools"][0]
        if len(self.run_calls) == 1:
            inputs = self._tool_inputs
        else:
            inputs = self._redrive_tool_inputs
        for tool_input in inputs or []:
            asyncio.run(emit.handle(_step(tool_input)))
        return object()

    def start_command(self, session_id, target):
        """Stand in for the RunRegistry: run the target inline and report started.

        Production registers ``target`` as a managed background run; the fake
        runs it synchronously so the re-drive logic is exercised deterministically
        (no thread to await). Returns True = the registry accepted the start.
        """
        target(threading.Event())
        return True


def test_responder_returns_validated_object_and_scopes_session():
    runtime = _FakeRuntime(tool_inputs=[{"name": "Ada"}])
    responder = StructuredResponder(
        runtime=runtime,
        schema=_PERSON_SCHEMA,
        workspace="my-wiki",
        allowed_tools=["wiki_search_pages"],
    )
    out = responder.run("Who wrote the first algorithm?")
    # Real emit path: handle() validated + stashed the payload, responder read it.
    assert out == {"name": "Ada"}
    # Workspace → wiki capability advertised.
    assert {"client_capabilities": ["wiki"]} in runtime.context_events
    # Strict scope + auto-approve + the emit tool injected.
    assert runtime.run_kwargs["strict_tool_scope"] is True
    assert runtime.run_kwargs["allowed_tools"] == ["wiki_search_pages"]
    assert runtime.run_kwargs["approval_callback"] is auto_approve
    injected = runtime.run_kwargs["extra_session_tools"]
    assert len(injected) == 1 and injected[0].tool_id == "emit_result"


def test_responder_stamps_structured_provenance_tag_and_surface():
    """``_prepare`` stamps a per-session ``structured:run:<id>`` tag + surface (#78/#87).

    Without this the session is untagged → ``SessionOrigin`` falls back to
    ``user`` and the trace loses ``surface:<platform>``. The tag is UNIQUE per
    session (``structured:run:<id>``), never the bare ``structured:run`` prefix —
    a constant tag would collide on the tag-keyed store and let one run steal
    every other run's tag (#87). This also covers the MCP ``structured_query``
    tool, which posts to the same route.
    """
    from mewbo_core.structured_response import STRUCTURED_RUN_TAG

    runtime = _FakeRuntime(tool_inputs=[{"name": "Ada"}])
    responder = StructuredResponder(
        runtime=runtime,
        schema=_PERSON_SCHEMA,
        source_platform="mcp",
    )
    responder.run("who?")
    # Unique per-session tag, NOT the bare prefix.
    assert f"{STRUCTURED_RUN_TAG}:sess-1" in runtime.tags
    assert STRUCTURED_RUN_TAG not in runtime.tags
    assert {"source_platform": "mcp"} in runtime.context_events
    # The tag classifies the session as ``structured``, not the ``user`` fallback
    # (the prefix-matching parser is transparent to the id segment).
    assert SessionOrigin.classify(runtime.tags, {}) == SessionOrigin.STRUCTURED


def test_responder_persists_structured_output_event_for_async_get():
    """A successful emit must write a ``structured_output`` event to the transcript.

    The async path (``start_async`` → ``GET /v1/structured``) reads the result
    back from the transcript via ``_load_structured_output`` — it does NOT see the
    in-memory ``emit.payload`` the sync ``run()`` relies on. So ``_prepare`` MUST
    wire the emit tool's ``event_logger`` to ``runtime.append_event``. Without it
    a *successful* emit produced no event and the async GET 422'd "model did not
    emit" despite the run succeeding (#40). This fails if the logger is unwired.
    """
    runtime = _FakeRuntime(tool_inputs=[{"name": "Ada", "age": 36}])
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    run_id = responder.start_async("who is ada")
    assert run_id  # started + ran inline via the fake start_command
    outputs = [e for e in runtime.events if e.get("type") == "structured_output"]
    assert len(outputs) == 1, "a successful emit must persist exactly one structured_output event"
    assert outputs[0]["payload"] == {"name": "Ada", "age": 36}
    # The run produced a result → NO terminal-failure event was recorded.
    assert not any(
        isinstance(e.get("payload"), dict) and e["payload"].get("done_reason") == "error"
        for e in runtime.events
    )


def test_responder_injects_force_emit_directive():
    """The structured run carries the force-emit system directive (no re-drive)."""
    runtime = _FakeRuntime(tool_inputs=[{"name": "Ada"}])
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    responder.run("q")
    si = runtime.run_kwargs["skill_instructions"]
    assert si is not None
    assert FORCE_EMIT_DIRECTIVE in si
    assert "emit_result" in si
    # A single, successful emit needs no re-drive.
    assert len(runtime.run_calls) == 1


def test_responder_redrives_once_when_emit_missing_then_succeeds():
    """First run yields nothing → one bounded re-drive forces the emit → success."""
    runtime = _FakeRuntime(tool_inputs=None, redrive_tool_inputs=[{"name": "Grace"}])
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    out = responder.run("q")
    assert out == {"name": "Grace"}
    # Exactly one re-drive (two run_sync calls total) — not a new control loop.
    assert len(runtime.run_calls) == 2
    # The re-drive injects a forcing directive too.
    assert FORCE_EMIT_DIRECTIVE in runtime.run_calls[1]["skill_instructions"]


def test_responder_reuses_same_emit_instance_across_redrive():
    """The re-drive reuses the SAME emit tool so its payload holder persists."""
    runtime = _FakeRuntime(tool_inputs=None, redrive_tool_inputs=[{"name": "Grace"}])
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    responder.run("q")
    first_emit = runtime.run_calls[0]["extra_session_tools"][0]
    second_emit = runtime.run_calls[1]["extra_session_tools"][0]
    assert first_emit is second_emit


def test_responder_raises_when_no_structured_output_emitted():
    # The model never calls emit_result on the first run NOR the re-drive →
    # payload stays None after the single bounded re-drive → raise.
    runtime = _FakeRuntime(tool_inputs=None, redrive_tool_inputs=None)
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    try:
        responder.run("q")
        raise AssertionError("expected StructuredResponseError")
    except StructuredResponseError as exc:
        assert "never called emit_result" in str(exc).lower()
    # One re-drive was attempted before giving up.
    assert len(runtime.run_calls) == 2


def test_responder_start_async_returns_run_id():
    """start_async returns a run_id and drives the session via the RunRegistry seam.

    The implementation calls ``runtime.start_command(session_id, target)`` —
    the same RunRegistry seam every other session run uses — so the run is
    serialized per session and visible to ``is_running``.  ``_FakeRuntime``
    supplies a synchronous ``start_command`` that executes the target inline,
    so the re-drive logic is exercised deterministically.  The returned run_id
    embeds the session_id (``"<session_id>:r1"``) so callers can correlate back.
    """
    done = threading.Event()
    orig_run_sync = _FakeRuntime.run_sync

    class _TrackedRuntime(_FakeRuntime):
        def run_sync(self, **kwargs):
            result = orig_run_sync(self, **kwargs)
            done.set()
            return result

    runtime = _TrackedRuntime(tool_inputs=[{"name": "Turing"}])
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    run_id = responder.start_async("q")
    # run_id encodes the session_id (returned by resolve_session = "sess-1").
    assert "sess-1" in run_id
    # Background thread completes; run_sync was called with the emit tool injected.
    done.wait(timeout=5)
    assert runtime.run_calls, "run_sync must have been called in the background thread"
    injected = runtime.run_calls[0]["extra_session_tools"]
    assert len(injected) == 1 and injected[0].tool_id == "emit_result"
    assert runtime.run_calls[0]["approval_callback"] is auto_approve
    assert runtime.run_calls[0]["strict_tool_scope"] is True
    assert FORCE_EMIT_DIRECTIVE in runtime.run_calls[0]["skill_instructions"]


def test_responder_raises_when_validation_fails_past_cap():
    # Every emit call is missing required "name"; max_failures=2 → 2nd call gives up.
    runtime = _FakeRuntime(tool_inputs=[{"age": 1}, {"age": 2}])
    responder = StructuredResponder(
        runtime=runtime, schema=_PERSON_SCHEMA, max_failures=2
    )
    try:
        responder.run("q")
        raise AssertionError("expected StructuredResponseError")
    except StructuredResponseError as exc:
        assert "validation failed" in str(exc).lower()


# ---------------------------------------------------------------------------
# Bug A: terminal_reason() contract — done_reason per-tool, not hardcoded
# ---------------------------------------------------------------------------


def test_emit_tool_terminal_reason_is_completed():
    """EmitStructuredResponseTool.terminal_reason() must return 'completed'.

    This is the cross-layer contract: a successful structured emit stamps
    done_reason='completed' (not 'awaiting_approval') so the API/MCP can
    distinguish plan-gate holds from real completions.  Fails today without
    the Bug A fix.
    """
    tool = EmitStructuredResponseTool(session_id="s1", schema=_PERSON_SCHEMA)
    assert tool.terminal_reason() == "completed"


def test_exit_plan_mode_terminal_reason_is_awaiting_approval():
    """ExitPlanModeTool.terminal_reason() must still return 'awaiting_approval'.

    Regression guard: the Bug A fix must NOT change plan-mode semantics.
    """
    tool = ExitPlanModeTool(session_id="s1")
    assert tool.terminal_reason() == "awaiting_approval"


def test_loop_done_reason_uses_terminating_tool_terminal_reason():
    """The ToolUseLoop done_reason must come from the terminating tool, not a literal.

    Drives the real loop with a fake SessionTool that (1) signals termination via
    should_terminate_run() and (2) returns 'my_custom_reason' from terminal_reason().
    Asserts done_reason matches the tool, not the hardcoded 'awaiting_approval'.
    Fails today without the Bug A fix to tool_use_loop.py.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    from langchain_core.messages import AIMessage
    from mewbo_core.agent_context import AgentContext
    from mewbo_core.common import MockSpeaker
    from mewbo_core.context import ContextSnapshot
    from mewbo_core.hooks import HookManager
    from mewbo_core.hypervisor import AgentHypervisor
    from mewbo_core.permissions import PermissionDecision, PermissionPolicy
    from mewbo_core.token_budget import TokenBudget
    from mewbo_core.tool_registry import ToolRegistry, ToolSpec
    from mewbo_core.tool_use_loop import ToolUseLoop

    # A fake SessionTool that terminates immediately with a custom reason.
    class _TerminatingTool:
        tool_id = "fake_terminal_tool"
        schema: dict = {
            "type": "function",
            "function": {
                "name": "fake_terminal_tool",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        modes: frozenset = frozenset({"act"})
        _pending: bool = False

        async def handle(self, action_step: ActionStep) -> MockSpeaker:  # type: ignore[override]
            self._pending = True
            return MockSpeaker(content="terminating")

        def should_terminate_run(self) -> bool:
            if self._pending:
                self._pending = False
                return True
            return False

        def terminal_reason(self) -> str:
            return "my_custom_reason"

    terminal_tool = _TerminatingTool()

    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        return_value=AIMessage(
            content="",
            tool_calls=[{"name": "fake_terminal_tool", "args": {}, "id": "c1"}],
        )
    )
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    registry = ToolRegistry()
    registry.register(ToolSpec(
        tool_id="fake_terminal_tool",
        name="fake_terminal_tool",
        description="test terminal tool",
        factory=lambda: MagicMock(),
        enabled=True,
        kind="local",
        metadata={"schema": {"type": "object", "properties": {}}},
    ))

    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW

    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision

    agent_ctx = AgentContext.root(
        model_name="test-model",
        max_depth=5,
        should_cancel=None,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=None,
    )
    ctx_snapshot = ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0, summary_tokens=0, event_tokens=0,
            context_window=128000, remaining_tokens=128000,
            utilization=0.0, threshold=0.8,
        ),
    )

    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=agent_ctx,
            tool_registry=registry,
            permission_policy=policy,
            hook_manager=hm,
            extra_session_tools=[terminal_tool],
        )

        _tq, state = asyncio.run(
            loop.run("trigger terminal", tool_specs=registry.list_specs(), context=ctx_snapshot)
        )

    assert state.done is True
    assert state.done_reason == "my_custom_reason", (
        f"Expected 'my_custom_reason' from terminal_reason(), got '{state.done_reason}'. "
        "Bug A is not fixed."
    )


# ---------------------------------------------------------------------------
# Bug B: start_async re-drive contract
# ---------------------------------------------------------------------------


def test_start_async_redrives_when_first_pass_misses_emit():
    """start_async re-drives once when the model omits emit_result on the first pass.

    The async path used to skip the belt-and-suspenders re-drive (Bug B). This
    test verifies the fix: first run_sync → no emit → second run_sync (re-drive)
    → emit → background thread exits; the structured_output event appears in the
    event log.  Fails today without the Bug B fix.
    """
    done = threading.Event()

    class _RedriveRuntime(_FakeRuntime):
        def run_sync(self, **kwargs):
            result = _FakeRuntime.run_sync(self, **kwargs)
            if len(self.run_calls) >= 2:
                done.set()
            return result

    # First call produces nothing; second (re-drive) emits successfully.
    runtime = _RedriveRuntime(
        tool_inputs=None,
        redrive_tool_inputs=[{"name": "Grace"}],
    )
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    run_id = responder.start_async("q")
    assert run_id  # non-empty

    done.wait(timeout=5)
    # Two run_sync calls: first drive + re-drive.
    assert len(runtime.run_calls) == 2, (
        f"Expected 2 run_sync calls (drive+redrive) but got {len(runtime.run_calls)}. "
        "Bug B is not fixed."
    )
    # The re-drive also carries the forcing directive.
    assert FORCE_EMIT_DIRECTIVE in runtime.run_calls[1]["skill_instructions"]
    # Success path: a result was produced → NO terminal-failure event appended.
    assert [e for e in runtime.events if e["type"] == "completion"] == []


def test_start_async_records_terminal_failure_when_emit_never_fires():
    """When neither drive triggers emit_result, start_async must persist a
    terminal FAILURE completion (not silently `completed`) so summarize_session
    reports `failed` and GET surfaces the reason — never a late phantom 422 (#40).
    """
    done = threading.Event()

    class _NeverEmitRuntime(_FakeRuntime):
        def run_sync(self, **kwargs):
            result = _FakeRuntime.run_sync(self, **kwargs)
            if len(self.run_calls) >= 2:
                done.set()
            return result

    runtime = _NeverEmitRuntime(tool_inputs=None, redrive_tool_inputs=None)
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    run_id = responder.start_async("q")
    assert run_id  # non-empty run_id returned immediately

    done.wait(timeout=5)
    # Two attempts were made (drive + re-drive); thread exited without raising.
    assert len(runtime.run_calls) == 2
    # A terminal-failure completion event was persisted via append_event.
    completions = [e for e in runtime.events if e["type"] == "completion"]
    assert len(completions) == 1
    payload = completions[0]["payload"]
    assert payload["done"] is False
    assert payload["done_reason"] == "error"
    assert "emit_result" in payload["reason"]


def test_start_async_records_terminal_failure_when_validation_cap_hit():
    """When the model emits but fails schema validation up to the cap,
    start_async persists a terminal failure whose reason names the validation
    failure — the emit.failed branch of _failure_reason (#40)."""
    done = threading.Event()

    class _InvalidRuntime(_FakeRuntime):
        def run_sync(self, **kwargs):
            result = _FakeRuntime.run_sync(self, **kwargs)
            done.set()
            return result

    # Two invalid payloads (missing required "name") in ONE run → with
    # max_failures=2 the second hits the cap → emit.failed=True (no re-drive).
    runtime = _InvalidRuntime(tool_inputs=[{"age": 1}, {"age": 2}])
    responder = StructuredResponder(
        runtime=runtime, schema=_PERSON_SCHEMA, max_failures=2
    )
    run_id = responder.start_async("q")
    assert run_id

    done.wait(timeout=5)
    completions = [e for e in runtime.events if e["type"] == "completion"]
    assert len(completions) == 1
    payload = completions[0]["payload"]
    assert payload["done"] is False
    assert payload["done_reason"] == "error"
    assert "validation failed" in payload["reason"].lower()
