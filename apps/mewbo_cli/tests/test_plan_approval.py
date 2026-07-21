#!/usr/bin/env python3
"""High-fidelity plan-approval tests.

Covers the three deliverables end-to-end without a live LLM:

- P1 — the ``plan_proposal`` renderer draws the ``plan.md`` markdown as a
  bordered "Proposed plan" card (not the planner's raw spawn/check JSON), with a
  long-plan preview cap.
- P2 — an interactive Approve / Keep-planning / Reject decision: the card footer
  + the ``PlanApprovalModal`` resolver (one cohesive surface, no slash commands).
- P3 — approve flips plan→act + drives an act-mode run; reject abandons; refine
  leaves the proposal pending. The plain-fallback ``/continue`` still approves.

Only the I/O boundary (``orchestrate_session``) is stubbed; the real
``SessionRuntime`` + ``SessionStore`` drive the plan_proposed/approved events.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mewbo_cli.cli_commands import get_registry
from mewbo_cli.cli_context import CliState, CommandContext
from mewbo_cli.tui.seams import MessageRendererRegistry, PermissionGateway, TranscriptItem
from mewbo_cli.tui.transcript_render import register_transcript_renderers
from mewbo_cli.tui.turn_engine import TurnEngine
from mewbo_core.classes import ActionStep, TaskQueue
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStore
from mewbo_core.tool_registry import ToolRegistry
from rich.console import Console

# --- helpers -------------------------------------------------------------


def _render_to_text(renderable: Any) -> str:
    """Render a Rich renderable to plain text for content assertions."""
    console = Console(record=True, width=100, color_system=None)
    console.print(renderable)
    return console.export_text()


def _propose_plan(
    store: SessionStore, session_id: str, plan_path: str, *, revision: int = 1
) -> None:
    """Append a ``plan_proposed`` event so the runtime sees a pending plan."""
    store.append_event(
        session_id,
        {
            "type": "plan_proposed",
            "payload": {"plan_path": plan_path, "revision": revision},
        },
    )


# --- P1/P2: the plan card renderer --------------------------------------


def test_plan_proposal_renders_bordered_card_with_decision_prompt() -> None:
    """The plan markdown becomes a titled card with the inline decision footer."""
    registry = MessageRendererRegistry()
    register_transcript_renderers(registry)
    item = TranscriptItem(
        "plan_proposal",
        {"text": "# Build it\n\n1. First step\n2. Second step", "revision": 2},
    )
    out = _render_to_text(registry.render(item))

    # P1 — the plan body + a clear "Proposed plan" card title (not raw JSON).
    assert "Proposed plan" in out
    assert "revision 2" in out
    assert "First step" in out
    # P2 — the three discoverable decision affordances + their modal keys.
    assert "Approve" in out
    assert "[a]" in out
    assert "Keep planning" in out
    assert "[k]" in out
    assert "Reject" in out
    assert "[r]" in out
    # No slash-command approval surface leaked into the card.
    assert "/approve" not in out
    assert "/reject" not in out


def test_plan_proposal_renderer_handles_empty_plan() -> None:
    registry = MessageRendererRegistry()
    register_transcript_renderers(registry)
    out = _render_to_text(registry.render(TranscriptItem("plan_proposal", {"text": "  "})))
    assert "empty plan" in out
    assert "Proposed plan" in out


def test_plan_proposal_long_plan_is_capped_with_hint() -> None:
    """A long plan is bounded with a '+N more lines' preview hint."""
    registry = MessageRendererRegistry()
    register_transcript_renderers(registry)
    body = "\n".join(f"- step {i}" for i in range(120))
    out = _render_to_text(registry.render(TranscriptItem("plan_proposal", {"text": body})))
    assert "more lines" in out
    assert "step 0" in out
    assert "step 119" not in out  # tail is hidden behind the preview cap


# --- no slash commands were added ---------------------------------------


def test_no_plan_slash_commands_registered() -> None:
    commands = get_registry().list_commands()
    assert "/approve" not in commands
    assert "/reject" not in commands


# --- P1/P3: TurnEngine surfaces the card, resolves the modal, acts -------


class _FakeCommandRegistry:
    def list_commands(self) -> list[str]:
        return ["/help"]

    def execute(self, name: str, context: Any, args: list[str]) -> bool:
        return True


class _FakeSkillRegistry:
    def get(self, name: str) -> Any:
        return None


def _engine(
    tmp_path: Any, *, resolver: Any = None
) -> tuple[TurnEngine, SessionStore, SessionRuntime, str, list[TranscriptItem]]:
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    runtime = SessionRuntime(session_store=store)
    state = CliState(session_id=session_id, show_plan=False, mode="plan")
    items: list[TranscriptItem] = []
    engine = TurnEngine(
        runtime=runtime,
        store=store,
        state=state,
        tool_registry=ToolRegistry(),
        command_registry=_FakeCommandRegistry(),
        skill_registry=_FakeSkillRegistry(),
        permission=PermissionGateway(auto_approve=lambda: True),
        hook_factory=lambda: None,
        emit=items.append,
        emit_renderable=lambda r: None,
        plan_approval_resolver=resolver,
    )
    return engine, store, runtime, session_id, items


def _plan_run(store: SessionStore, session_id: str, plan_path: str):
    """An orchestrate stub that proposes a plan via spawn/check tool steps."""

    def fake_orchestrate(*args: Any, **kwargs: Any) -> TaskQueue:
        if kwargs.get("mode") == "plan":
            _propose_plan(store, session_id, plan_path)
            return TaskQueue(
                action_steps=[
                    ActionStep(tool_id="spawn_agent", operation="run", tool_input="{}"),
                    ActionStep(tool_id="check_agents", operation="run", tool_input="{}"),
                ]
            )
        tq = TaskQueue(action_steps=[])
        tq.task_result = "implemented the plan"
        return tq

    return fake_orchestrate


def test_plan_card_emitted_and_raw_tool_json_suppressed(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no resolver, a pending plan emits the card + notice, not tool JSON."""
    engine, store, _runtime, session_id, items = _engine(tmp_path, resolver=None)
    plan_path = str(tmp_path / "plan.md")
    (tmp_path / "plan.md").write_text("# Plan\n\n1. Do the thing")
    monkeypatch.setattr(
        "mewbo_core.session_runtime.orchestrate_session",
        _plan_run(store, session_id, plan_path),
    )

    engine.run_query("add a feature")

    kinds = [i.kind for i in items]
    assert "plan_proposal" in kinds
    assert "tool" not in kinds  # raw spawn/check JSON suppressed
    proposal = next(i for i in items if i.kind == "plan_proposal")
    assert "Do the thing" in str(proposal.payload.get("text"))
    notice = next(i for i in items if i.kind == "notice")
    assert "/continue" in str(notice.payload.get("text"))


def test_modal_approve_transitions_plan_to_act_and_runs(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def resolver(plan_markdown: str, revision: int) -> str:
        seen["markdown"] = plan_markdown
        seen["revision"] = revision
        return "approve"

    engine, store, _runtime, session_id, items = _engine(tmp_path, resolver=resolver)
    plan_path = str(tmp_path / "plan.md")
    (tmp_path / "plan.md").write_text("# Plan\n\n1. Do the thing")
    monkeypatch.setattr(
        "mewbo_core.session_runtime.orchestrate_session",
        _plan_run(store, session_id, plan_path),
    )

    engine.run_query("add a feature")

    # The modal saw the real plan markdown; approval flipped plan→act + ran it.
    assert "Do the thing" in seen["markdown"]
    assert engine.state.mode == "act"
    types = [e.get("type") for e in store.load_transcript(session_id)]
    assert "plan_approved" in types
    # The act-mode result rendered with full transcript fidelity (assistant turn).
    assistant = next(i for i in items if i.kind == "assistant")
    assert "implemented the plan" in str(assistant.payload.get("text"))


def test_modal_reject_emits_plan_rejected(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, _runtime, session_id, items = _engine(
        tmp_path, resolver=lambda md, rev: "reject"
    )
    plan_path = str(tmp_path / "plan.md")
    (tmp_path / "plan.md").write_text("# Plan")
    monkeypatch.setattr(
        "mewbo_core.session_runtime.orchestrate_session",
        _plan_run(store, session_id, plan_path),
    )

    engine.run_query("add a feature")

    assert engine.state.mode == "plan"  # reject leaves us dormant in plan mode
    types = [e.get("type") for e in store.load_transcript(session_id)]
    assert "plan_rejected" in types
    assert any("reject" in str(i.payload.get("text", "")).lower() for i in items)


def test_modal_refine_leaves_plan_pending(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, store, runtime, session_id, items = _engine(
        tmp_path, resolver=lambda md, rev: "refine"
    )
    plan_path = str(tmp_path / "plan.md")
    (tmp_path / "plan.md").write_text("# Plan")
    monkeypatch.setattr(
        "mewbo_core.session_runtime.orchestrate_session",
        _plan_run(store, session_id, plan_path),
    )

    engine.run_query("add a feature")

    # Refine = keep planning: no approve/reject event, proposal stays pending.
    types = [e.get("type") for e in store.load_transcript(session_id)]
    assert "plan_approved" not in types
    assert "plan_rejected" not in types
    pending, _rev, _path = runtime._has_pending_plan_proposal(session_id)
    assert pending is True
    assert any("plann" in str(i.payload.get("text", "")).lower() for i in items)


# --- P3: the plain-fallback /continue approve path ----------------------


def test_continue_approves_when_a_plan_is_pending(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The no-TTY plain fallback: /continue approves + executes a pending plan."""
    store = SessionStore(root_dir=str(tmp_path))
    session_id = store.create_session()
    runtime = SessionRuntime(session_store=store)
    state = CliState(session_id=session_id, show_plan=False, mode="plan")
    ctx = CommandContext(
        console=Console(record=True),
        store=store,
        state=state,
        tool_registry=ToolRegistry(),
        runtime=runtime,
        prompt_func=None,
    )
    plan_path = str(tmp_path / "plan.md")
    (tmp_path / "plan.md").write_text("# Plan")
    _propose_plan(store, session_id, plan_path)

    captured: dict[str, Any] = {}

    def fake_orchestrate(*args: Any, **kwargs: Any) -> TaskQueue:
        captured.update(kwargs)
        return TaskQueue(action_steps=[])

    monkeypatch.setattr("mewbo_core.session_runtime.orchestrate_session", fake_orchestrate)

    assert get_registry().execute("/continue", ctx, []) is True

    assert ctx.state.mode == "act"
    assert captured.get("mode") == "act"
    types = [e.get("type") for e in store.load_transcript(session_id)]
    assert "plan_approved" in types


# --- P2: PlanApprovalModal Pilot tests (real key bindings → dismiss value) ---


def _make_plan_modal_app(plan_markdown: str = "# Plan\n\n1. Step") -> tuple[Any, list[str]]:
    """Build a tiny App that pushes the PlanApprovalModal and collects the result."""
    from mewbo_cli.cli_theme import DEFAULT_PALETTE
    from mewbo_cli.tui.widgets.plan_modal import PlanApprovalModal
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    results: list[str] = []

    class _ModalTestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("modal host")

        def on_mount(self) -> None:
            modal = PlanApprovalModal(plan_markdown, 1, palette=DEFAULT_PALETTE)
            self.push_screen(modal, callback=lambda v: results.append(v))

    return _ModalTestApp(), results


@pytest.mark.parametrize(
    ("key", "expected"),
    [("a", "approve"), ("k", "refine"), ("r", "reject"), ("escape", "refine")],
)
def test_plan_modal_keys_dismiss_with_decision(key: str, expected: str) -> None:
    """a=approve · k/esc=refine (safe default) · r=reject."""

    async def _run() -> None:
        app, results = _make_plan_modal_app()
        async with app.run_test() as pilot:
            await pilot.pause()  # let on_mount push the modal
            await pilot.press(key)
            await pilot.pause()
        assert results == [expected]

    asyncio.run(_run())


def test_plan_modal_constructs_without_live_app() -> None:
    from mewbo_cli.cli_theme import DEFAULT_PALETTE
    from mewbo_cli.tui.widgets.plan_modal import PlanApprovalModal
    from textual.screen import ModalScreen

    modal = PlanApprovalModal("# Plan", 2, palette=DEFAULT_PALETTE)
    assert isinstance(modal, ModalScreen)
