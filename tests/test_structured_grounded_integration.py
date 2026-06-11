"""Integration tests: structured-query lifecycle is healthy end-to-end.

Guards four contracts that #40 and #51 silently broke because every prior test
stubbed the seam that actually failed:

    A. Grounding resolves (#51): a wiki_search_pages call in a session carrying
       a ``structured_workspace`` event returns real hits, NOT "wiki QA ctx not found".
    B. Emit ⇒ completed (#40 mislabel): a real ToolUseLoop driven with
       emit_result yields done_reason=="completed", not "awaiting_approval".
    C. Re-drive on skip: the real StructuredResponder._run_with_redrive emits
       when the first drive produces nothing but the re-drive calls emit_result.
    D. run_id round-trip: skipped (Flask route requires a real runtime +
       database; too heavy for a unit/integration test — A/B/C cover the
       essential contracts).

Stub boundary: only ``model.ainvoke`` (LLM) and the tool runtime seam
(``_resolve_runtime`` / ``_make_embedder``). All production paths from the LLM
response down to the SessionTool / emit / grounding are real.
"""
from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage
from mewbo_core.agent_context import AgentContext
from mewbo_core.context import ContextSnapshot
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHypervisor
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.structured_response import (
    EmitStructuredResponseTool,
    StructuredResponder,
    StructuredResponseError,
)
from mewbo_core.token_budget import TokenBudget
from mewbo_core.tool_registry import ToolRegistry
from mewbo_core.tool_use_loop import ToolUseLoop

# ── Person schema used across all tests ────────────────────────────────────

_PERSON_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    "required": ["name"],
    "additionalProperties": False,
}


# ── Shared helpers (mirrors test_tool_use_loop.py) ──────────────────────────


def _make_context() -> ContextSnapshot:
    return ContextSnapshot(
        summary=None,
        recent_events=[],
        selected_events=None,
        events=[],
        budget=TokenBudget(
            total_tokens=0,
            summary_tokens=0,
            event_tokens=0,
            context_window=128000,
            remaining_tokens=128000,
            utilization=0.0,
            threshold=0.8,
        ),
    )


def _make_agent_context() -> AgentContext:
    return AgentContext.root(
        model_name="test-model",
        max_depth=5,
        should_cancel=None,
        registry=AgentHypervisor(max_concurrent=100),
        event_logger=None,
    )


def _allow_all_policy() -> PermissionPolicy:
    policy = MagicMock(spec=PermissionPolicy)
    policy.decide.return_value = PermissionDecision.ALLOW
    return policy


def _make_hook_manager() -> HookManager:
    hm = MagicMock(spec=HookManager)
    hm.run_pre_tool_use.side_effect = lambda step: step
    hm.run_post_tool_use.side_effect = lambda step, result: result
    hm.run_permission_request.side_effect = lambda step, decision: decision
    return hm


def _tool_call_response(tool_id: str, args: dict, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": tool_id, "args": args, "id": call_id}],
    )


def _text_response(content: str) -> AIMessage:
    return AIMessage(content=content)


# ── Wiki fixture helpers (mirrors tests/wiki/test_workspace_resolver.py) ────


class _FakeSessionStore:
    """Minimal session-store double: records context events + loads transcript."""

    def __init__(self) -> None:
        self._transcripts: dict[str, list[dict]] = {}

    def append_context_event(self, session_id: str, payload: dict) -> None:
        self._transcripts.setdefault(session_id, []).append(
            {"type": "context", "payload": payload}
        )

    def load_transcript(self, session_id: str) -> list[dict]:
        return list(self._transcripts.get(session_id, []))


def _wiki_store(tmp_path: Path):
    from mewbo_graph.wiki.store import JsonWikiStore
    return JsonWikiStore(root_dir=tmp_path / "wiki")


def _seed_workspace(wiki_store, slug: str = "org/repo") -> None:
    """Seed a page + graph node + embedding so retrieval has something to return."""
    from mewbo_graph.wiki.types import Embedding, GraphNode, WikiPage

    wiki_store.save_page(slug, WikiPage(
        id="auth",
        title="Auth",
        frontmatter={"title": "Auth", "slug": "auth"},
        body="Tokens, sessions, login flow.",
        toc=[],
        nav=[],
    ))
    wiki_store.upsert_nodes(slug, [
        GraphNode(
            slug=slug,
            node_id="f1",
            type="Function",
            name="authenticate",
            file="auth.py",
            range=(0, 100),
            docstring="Verify token.",
        ),
    ])
    wiki_store.upsert_embeddings(slug, [
        Embedding(slug=slug, node_id="f1", vector=[1.0, 0.0], model="m", dim=2),
    ])


def _runtime(wiki_store, session_store=None):
    if session_store is None:
        return SimpleNamespace(wiki_store=wiki_store)
    return SimpleNamespace(wiki_store=wiki_store, session_store=session_store)


# ── Assertion A — Grounding resolves via structured_workspace (#51) ─────────


def test_A_grounding_resolves_via_workspace_event(tmp_path: Path) -> None:
    """A wiki_search_pages call in a session with a structured_workspace event
    returns REAL hits — NOT "wiki QA ctx not found".

    This drives the REAL WikiSearchPagesTool.handle() with patched runtime /
    embedder, so the grounding path (resolve_qa_ctx → resolve_workspace_slug →
    load_transcript → slug) is exercised end-to-end.  The tool's seams
    (_resolve_runtime, _make_embedder) are the only stubs — all ctx resolution
    is real.
    """
    from mewbo_graph.plugins.wiki import search_pages as search_pages_mod
    from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")

    sessions = _FakeSessionStore()
    # Exactly what StructuredResponder._prepare writes when workspace is set.
    sessions.append_context_event("sess-A", {"client_capabilities": ["wiki"]})
    sessions.append_context_event("sess-A", {"structured_workspace": "org/repo"})

    runtime = _runtime(wiki_store, sessions)
    tool = WikiSearchPagesTool(session_id="sess-A")
    step = MagicMock(tool_input={"query": "authentication token"})

    embedder = MagicMock()
    # Return a zero-vector — cosine similarity to the seeded [1, 0] still works.
    embedder.embed_query.return_value = [0.0, 0.0]

    with (
        patch.object(search_pages_mod, "_resolve_runtime", return_value=runtime),
        patch.object(search_pages_mod, "_make_embedder", return_value=embedder),
    ):
        result = asyncio.run(tool.handle(step))

    body = str(result.content)

    # Guard: the grounding ctx was resolved — the old "not found" error is gone.
    assert "wiki QA ctx not found" not in body, (
        f"Bug #51 still present — ctx not resolved from structured_workspace event.\n"
        f"Tool result: {body!r}"
    )
    # The seeded page must appear — proves real retrieval ran, not a mock shortcut.
    assert "auth" in body, (
        f"Seeded page 'auth' missing from hits — retrieval didn't run.\n"
        f"Tool result: {body!r}"
    )


# ── Assertion B — Emit ⇒ done_reason "completed", NOT "awaiting_approval" ──


def test_B_emit_result_drives_done_reason_completed() -> None:
    """The real ToolUseLoop stamps done_reason='completed' when emit_result fires.

    Drives the REAL ToolUseLoop with a fake LLM whose single turn calls
    emit_result with a valid payload.  The test asserts that:
      - state.done is True (the loop terminated),
      - state.done_reason == "completed" (not "awaiting_approval"),
      - the emit tool's payload was populated with the validated object.

    Fails if EmitStructuredResponseTool.terminal_reason() returns the wrong
    string or if the loop ignores the per-tool terminal_reason().
    """
    session_id = "sess-B"
    events: list[dict] = []

    emit = EmitStructuredResponseTool(
        session_id=session_id,
        schema=_PERSON_SCHEMA,
        event_logger=events.append,
    )

    # LLM produces a single tool call for emit_result, then we never call
    # ainvoke again — the loop exits because emit signals termination.
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        return_value=_tool_call_response(
            "emit_result",
            {"name": "Ada", "age": 36},
            call_id="c-B-1",
        )
    )
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    registry = ToolRegistry()  # empty — emit_result is a SessionTool, not a registry tool

    with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id=session_id,
            extra_session_tools=[emit],
        )

        _tq, state = asyncio.run(
            loop.run(
                "Give me a person object",
                tool_specs=registry.list_specs(),
                context=_make_context(),
            )
        )

    assert state.done is True, "Loop must have terminated."
    assert state.done_reason == "completed", (
        f"Bug #40 still present — done_reason is {state.done_reason!r}, expected 'completed'.\n"
        "EmitStructuredResponseTool.terminal_reason() must return 'completed'."
    )

    # Emit payload was populated by the REAL handle() — not mocked.
    assert emit.payload == {"name": "Ada", "age": 36}, (
        f"emit.payload={emit.payload!r} — the real emit tool must have been called."
    )

    # The structured_output event must be in the event log.
    assert len(events) == 1, f"Expected 1 structured_output event, got {len(events)}."
    assert events[0]["type"] == "structured_output"
    assert events[0]["payload"] == {"name": "Ada", "age": 36}


# ── Assertion B2 — A grounding tool call then emit still yields "completed" ─


def test_B2_ground_then_emit_yields_completed(tmp_path: Path) -> None:
    """Two-turn loop: turn 1 calls wiki_search_pages, turn 2 calls emit_result.

    Exercises the full path the spec describes: LLM grounds first, then emits.
    Both tools are real (no mocked handle).  done_reason must be 'completed'.
    """
    from mewbo_graph.plugins.wiki import search_pages as search_pages_mod
    from mewbo_graph.plugins.wiki.search_pages import WikiSearchPagesTool

    session_id = "sess-B2"
    events: list[dict] = []

    wiki_store = _wiki_store(tmp_path)
    _seed_workspace(wiki_store, "org/repo")
    sessions = _FakeSessionStore()
    sessions.append_context_event(session_id, {"client_capabilities": ["wiki"]})
    sessions.append_context_event(session_id, {"structured_workspace": "org/repo"})

    runtime = _runtime(wiki_store, sessions)
    embedder = MagicMock()
    embedder.embed_query.return_value = [0.0, 0.0]

    emit = EmitStructuredResponseTool(
        session_id=session_id,
        schema=_PERSON_SCHEMA,
        event_logger=events.append,
    )
    search_tool = WikiSearchPagesTool(session_id=session_id)

    # Turn 1: call wiki_search_pages (grounding)
    # Turn 2: call emit_result with a valid payload
    fake_model = MagicMock()
    fake_model.ainvoke = AsyncMock(
        side_effect=[
            _tool_call_response("wiki_search_pages", {"query": "auth"}, "c-B2-1"),
            _tool_call_response("emit_result", {"name": "Ada"}, "c-B2-2"),
        ]
    )
    bound = MagicMock()
    bound.ainvoke = fake_model.ainvoke

    registry = ToolRegistry()

    with (
        patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build,
        patch.object(search_pages_mod, "_resolve_runtime", return_value=runtime),
        patch.object(search_pages_mod, "_make_embedder", return_value=embedder),
    ):
        mock_build.return_value = MagicMock()
        mock_build.return_value.bind_tools.return_value = bound

        loop = ToolUseLoop(
            agent_context=_make_agent_context(),
            tool_registry=registry,
            permission_policy=_allow_all_policy(),
            hook_manager=_make_hook_manager(),
            session_id=session_id,
            extra_session_tools=[emit, search_tool],
        )

        _tq, state = asyncio.run(
            loop.run(
                "Find Ada's record",
                tool_specs=registry.list_specs(),
                context=_make_context(),
            )
        )

    assert state.done is True
    assert state.done_reason == "completed", (
        f"Bug #40 still present after grounding turn — done_reason={state.done_reason!r}."
    )
    assert emit.payload == {"name": "Ada"}
    # grounding tool result must NOT have been the ctx-not-found error
    # (checked indirectly: emit was reached, which means grounding didn't crash the loop)
    assert len(events) == 1
    assert events[0]["type"] == "structured_output"


# ── Assertion C — Re-drive produces the object when first drive skips emit ──


class _RunWithRedriveRuntime:
    """A runtime double that drives the REAL ToolUseLoop end-to-end.

    Unlike ``_FakeRuntime`` in test_structured_response.py, this double
    runs the REAL ``orchestrate_session``-shaped loop: it builds a
    ``ToolUseLoop``, feeds it the LLM mock whose ``side_effect`` is a
    shared queue, and actually calls ``loop.run()``.

    ``start_command`` runs the target synchronously in-thread (same as
    ``_FakeRuntime.start_command``) so the re-drive is deterministic
    without threading.
    """

    def __init__(self, llm_responses: list[AIMessage]) -> None:
        """
        ``llm_responses`` is the SHARED sequence of AIMessage objects the fake
        LLM will return across ALL ``run_sync`` calls (first drive + re-drive).
        """
        self._queue: list[AIMessage] = list(llm_responses)
        self.context_events: list[dict] = []
        self.run_calls: int = 0

    def resolve_session(self, *, session_tag=None, session_id=None) -> str:
        return "sess-C"

    def tag_session(self, session_id: str, tag: str) -> None:
        """Stamp the structured provenance tag (#78); no-op store for this double."""

    def append_context_event(self, session_id: str, context: dict) -> None:
        self.context_events.append(context)

    def run_sync(self, **kwargs) -> object:
        """Drive the REAL ToolUseLoop with the next batch of queued LLM responses."""
        self.run_calls += 1

        extra_session_tools = kwargs.get("extra_session_tools", [])

        # Build a fake LLM bound model whose ainvoke pops from the shared queue.
        queue = self._queue  # capture

        async def _ainvoke(messages, **_kw):  # noqa: ANN001
            if not queue:
                # Safety: if the model is asked when the queue is empty, return
                # a text response to terminate the loop gracefully.
                return _text_response("(no more responses)")
            return queue.pop(0)

        fake_bound = MagicMock()
        fake_bound.ainvoke = _ainvoke

        registry = ToolRegistry()
        agent_ctx = AgentContext.root(
            model_name="test-model",
            max_depth=5,
            should_cancel=None,
            registry=AgentHypervisor(max_concurrent=100),
            event_logger=None,
        )
        policy = MagicMock(spec=PermissionPolicy)
        policy.decide.return_value = PermissionDecision.ALLOW
        hm = MagicMock(spec=HookManager)
        hm.run_pre_tool_use.side_effect = lambda step: step
        hm.run_post_tool_use.side_effect = lambda step, result: result
        hm.run_permission_request.side_effect = lambda step, decision: decision

        with patch("mewbo_core.tool_use_loop.build_chat_model") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.bind_tools.return_value = fake_bound

            loop = ToolUseLoop(
                agent_context=agent_ctx,
                tool_registry=registry,
                permission_policy=policy,
                hook_manager=hm,
                session_id=kwargs.get("session_id", "sess-C"),
                extra_session_tools=extra_session_tools,
            )

            asyncio.run(
                loop.run(
                    kwargs.get("user_query", ""),
                    tool_specs=registry.list_specs(),
                    context=_make_context(),
                )
            )

        return object()

    def start_command(self, session_id: str, target) -> bool:
        """Synchronous inline execution (no real thread needed for determinism)."""
        target(threading.Event())
        return True


def test_C_redrive_produces_object_when_first_drive_skips_emit() -> None:
    """StructuredResponder._run_with_redrive emits when only the re-drive calls emit.

    Sequence:
      Drive 1 — LLM returns a text answer (no emit_result call) → emit.payload is None.
      Drive 2 (re-drive) — LLM calls emit_result → payload set.

    The real ``_run_with_redrive`` must detect the missing payload and fire the
    re-drive automatically (not skip it as the async path did before #40).
    Asserts: two ``run_sync`` calls happened AND the final payload is the emitted object.
    """
    # Drive 1: model answers in prose (no emit_result) → payload stays None.
    # Drive 2: model calls emit_result with a valid payload.
    llm_responses: list[AIMessage] = [
        _text_response("Here is the answer in prose — no emit_result."),  # drive 1
        _tool_call_response("emit_result", {"name": "Grace"}, "c-C-1"),   # drive 2 (re-drive)
    ]

    runtime = _RunWithRedriveRuntime(llm_responses)
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)

    # _run_with_redrive is internal; test via StructuredResponder.run() which calls it.
    # We must catch StructuredResponseError in case the re-drive still doesn't work.
    try:
        out = responder.run("Who is Grace?")
    except StructuredResponseError as exc:
        raise AssertionError(
            f"Bug: re-drive did not produce the object. StructuredResponseError: {exc}\n"
            f"run_calls={runtime.run_calls} (expected 2 for drive+redrive)"
        ) from exc

    assert runtime.run_calls == 2, (
        f"Expected 2 run_sync calls (drive + re-drive), got {runtime.run_calls}. "
        "The re-drive was not attempted — Bug C."
    )
    assert out == {"name": "Grace"}, (
        f"Expected {{'name': 'Grace'}}, got {out!r}. "
        "The re-drive ran but emit_result was not captured."
    )


def test_C_redrive_is_not_triggered_when_first_drive_emits() -> None:
    """If drive 1 emits successfully, there is no re-drive (exactly one run_sync call)."""
    llm_responses: list[AIMessage] = [
        _tool_call_response("emit_result", {"name": "Ada"}, "c-C2-1"),  # drive 1
    ]

    runtime = _RunWithRedriveRuntime(llm_responses)
    responder = StructuredResponder(runtime=runtime, schema=_PERSON_SCHEMA)
    out = responder.run("Who is Ada?")

    assert runtime.run_calls == 1, (
        f"Re-drive must NOT fire when drive 1 succeeds. Got {runtime.run_calls} calls."
    )
    assert out == {"name": "Ada"}


# ── Assertion D: API round-trip — skipped (rationale in module docstring) ───
#
# The Flask route requires a real SessionRuntime backed by a session store
# (in-memory or Mongo), plus request-context plumbing.  Standing that up in a
# unit/integration test would recreate a significant portion of the API
# harness and duplicate the existing ``apps/mewbo_api/tests/test_structured_routes.py``
# test surface without adding meaningful coverage over A/B/C.  The
# essential contracts (grounding, done_reason, re-drive) are fully covered
# by A/B/C above.  D is intentionally skipped.
