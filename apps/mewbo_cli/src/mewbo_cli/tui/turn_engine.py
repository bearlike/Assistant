#!/usr/bin/env python3
"""TurnEngine — the App-agnostic dispatch + query core (issue #150).

Ports the ``run_cli`` REPL body and ``_run_query`` from the ``cli_master``
free-function monolith into one injectable atomic class. It owns the dispatch
decision (command vs skill vs query) and a single turn against an injected
``SessionRuntime`` (DI — core is never modified), emitting transcript content
through two injected callbacks so it is agnostic to *how* output is shown:

- ``emit(TranscriptItem)`` — structured content rendered via the message
  registry seam (``user`` / ``assistant`` / ``tool`` / ``plan`` / ``notice``).
- ``emit_renderable(RenderableType)`` — pre-rendered Rich content (captured
  command output, resilience/usage notices) appended verbatim.

``MewboApp`` wraps both callbacks with ``call_from_thread`` and runs
:meth:`handle` on a worker thread so the UI never blocks.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol

from mewbo_core.hooks import HookManager
from mewbo_core.session_runtime import SessionRuntime
from mewbo_core.session_store import SessionStoreBase
from mewbo_core.skills import activate_skill
from mewbo_core.task_master import generate_action_plan
from mewbo_core.tool_registry import ToolRegistry
from mewbo_tools.integration.reference_expansion import expand_references
from rich.console import Console
from rich.text import Text

from mewbo_cli.cli_context import PLAN_APPROVE_CONTINUATION, CliState, CommandContext
from mewbo_cli.cli_notices import (
    maybe_print_recovery_hint,
    print_resilience_events,
)
from mewbo_cli.tui.seams import PermissionGateway, TranscriptItem

if TYPE_CHECKING:
    from rich.console import RenderableType

# Tool-id families that drive per-type tool-card rendering in the transcript.
_BASH_TOOL_IDS = frozenset({"bash", "shell", "run_shell_command", "execute_bash"})
# Name hints (substring) so vendored shell ids (e.g. ``aider_shell_tool``) are
# recognised as shell executions and get their command extracted.
_BASH_TOOL_HINTS = ("shell", "bash", "terminal")
_BASH_COMMAND_KEYS = ("command", "cmd", "script")
_FILE_PATH_KEYS = ("file_path", "path", "filename", "file")
_OLD_TEXT_KEYS = ("old_string", "old_text", "old_str", "old")
_NEW_TEXT_KEYS = ("new_string", "new_text", "new_str", "new")
_CONTENT_KEYS = ("content", "new_content", "text")
# Keys whose values are large blobs we never inline into the compact header.
_BLOB_KEYS = frozenset(
    {
        "content",
        "new_content",
        "old_string",
        "new_string",
        "old_text",
        "new_text",
        "old_str",
        "new_str",
        "diff",
        "patch",
    }
)
_ARGS_SUMMARY_MAX = 60


class CommandRegistryLike(Protocol):
    """The slice of ``cli_commands.CommandRegistry`` the engine depends on."""

    def list_commands(self) -> list[str]:
        """Return the registered command tokens (each leading with ``/``)."""
        ...

    def execute(self, name: str, context: CommandContext, args: list[str]) -> bool:
        """Run command ``name``; return ``False`` to end the REPL loop."""
        ...


class SkillRegistryLike(Protocol):
    """The slice of ``SkillRegistry`` the engine depends on for dispatch."""

    def get(self, name: str) -> Any:
        """Return the skill named ``name``, or ``None`` if absent."""
        ...


class TurnEngine:
    """Classify one input line and run it against the session runtime.

    Dispatch order mirrors ``run_cli``: a registered command wins, then a
    user-invocable skill, then a plain query.
    """

    def __init__(
        self,
        *,
        runtime: SessionRuntime,
        store: SessionStoreBase,
        state: CliState,
        tool_registry: ToolRegistry,
        command_registry: CommandRegistryLike,
        skill_registry: SkillRegistryLike,
        permission: PermissionGateway,
        hook_factory: Callable[[], HookManager | None],
        emit: Callable[[TranscriptItem], None],
        emit_renderable: Callable[[RenderableType], None],
        plan_approval_resolver: Callable[[str, int], str] | None = None,
        max_iters: int = 3,
        session_step_budget: int = 0,
        no_color: bool = False,
        live: bool = False,
        cwd: Callable[[], str] = os.getcwd,
        console_factory: Callable[[], Console] | None = None,
    ) -> None:
        """Wire the runtime, registries, permission seam and emit callbacks.

        ``plan_approval_resolver(plan_markdown, revision) -> decision`` bridges a
        pending plan proposal to the App's plan-approval modal (mirrors the
        permission-modal resolver). It returns ``"approve"`` / ``"refine"`` /
        ``"reject"``. ``None`` (the foundation default / no-TTY) leaves the plan
        pending so the plain-fallback ``/continue`` can approve it.
        """
        self.runtime = runtime
        self.store = store
        self.state = state
        self.tool_registry = tool_registry
        self.command_registry = command_registry
        self.skill_registry = skill_registry
        self.permission = permission
        self._hook_factory = hook_factory
        self._plan_approval_resolver = plan_approval_resolver
        self._emit = emit
        self._emit_renderable = emit_renderable
        self._max_iters = max_iters
        self._session_step_budget = session_step_budget
        # ``live``: the App path streams the turn into the transcript live via the
        # AgentTranscriptHub (bus-driven), so the post-run batch dump of tool
        # cards + the final assistant block is SUPPRESSED — emitting it would
        # duplicate (and re-order, bucketed-by-type) what already streamed. The
        # plain-REPL / no-TTY fallback leaves ``live=False`` and keeps the dump.
        self._live = live
        self._cwd = cwd
        self._console_factory = console_factory or (
            lambda: Console(
                record=True,
                width=100,
                color_system=None if no_color else "truecolor",
            )
        )

    # -- classification ---------------------------------------------------

    @staticmethod
    def _parse_command(text: str) -> tuple[str, list[str]]:
        parts = text.strip().split()
        return parts[0], parts[1:]

    def classify(self, text: str) -> str:
        """Return ``empty`` | ``command`` | ``skill`` | ``unknown`` | ``query``."""
        stripped = text.strip()
        if not stripped:
            return "empty"
        if not stripped.startswith("/"):
            return "query"
        command = stripped.split()[0]
        if command in self.command_registry.list_commands():
            return "command"
        skill = self.skill_registry.get(command.lstrip("/"))
        if skill is not None and getattr(skill, "user_invocable", False):
            return "skill"
        return "unknown"

    # -- dispatch ---------------------------------------------------------

    def handle(self, text: str) -> bool:
        """Dispatch one input line. Returns ``False`` only to quit the app."""
        kind = self.classify(text)
        if kind == "empty":
            return True
        if kind == "command":
            command, args = self._parse_command(text)
            return self._run_command(command, args)
        if kind == "skill":
            command, args = self._parse_command(text)
            name = command.lstrip("/")
            skill = self.skill_registry.get(name)
            instructions, _ = activate_skill(skill, " ".join(args))
            self._emit(TranscriptItem("user", {"text": text}))
            self._emit(TranscriptItem("notice", {"text": f"Activating skill: {name}"}))
            self.run_query(text, skill_instructions=instructions)
            return True
        if kind == "unknown":
            self._emit(
                TranscriptItem(
                    "notice",
                    {"text": "Unknown command. Use /help for commands or /skills for skills."},
                )
            )
            return True
        # query
        self._emit(TranscriptItem("user", {"text": text}))
        self.run_query(text)
        return True

    def _run_command(self, command: str, args: list[str]) -> bool:
        console = self._console_factory()
        context = CommandContext(
            console=console,
            store=self.store,
            state=self.state,
            tool_registry=self.tool_registry,
            runtime=self.runtime,
            prompt_func=None,  # interactive command dialogs land with #155/#157
            # Let plan-approval commands (/approve, /continue) drive an act-mode
            # run with the App's permission modal + lifecycle hooks (#159).
            approval_callback=self.permission,
            hook_factory=self._hook_factory,
        )
        keep = True
        try:
            keep = self.command_registry.execute(command, context, args)
        except Exception as exc:  # noqa: BLE001 — a bad command must not kill the app
            console.print(f"[red]Command failed: {exc}[/red]")
        self._flush(console)
        return keep

    # -- query turn -------------------------------------------------------

    def run_query(self, query: str, *, skill_instructions: str | None = None) -> Any:
        """Run a single query turn, emitting plan, tools, response and notices."""
        query = expand_references(query, self._cwd()) or query
        mode = self.state.mode if self.state.mode in {"plan", "act"} else "act"

        initial_plan = None
        if self.state.show_plan and mode != "plan":
            try:
                initial_plan = generate_action_plan(
                    user_query=query,
                    model_name=self.state.model_name,
                    session_summary=self.store.load_summary(self.state.session_id),
                    mode=mode,
                )
            except Exception:  # noqa: BLE001 — plan preview is best-effort
                initial_plan = None
            if initial_plan is not None:
                self._emit(TranscriptItem("plan", {"plan": initial_plan}))

        task_queue = self.runtime.run_sync(
            user_query=query,
            model_name=self.state.model_name,
            fallback_models=self.state.fallback_models,
            max_iters=self._max_iters,
            initial_plan=initial_plan,
            session_id=self.state.session_id,
            tool_registry=self.tool_registry,
            approval_callback=self.permission,
            hook_manager=self._hook_factory() or HookManager(),
            mode=mode,
            skill_instructions=skill_instructions,
            session_step_budget=self._session_step_budget,
            source_platform="cli",
        )

        # A plan-mode run that halted awaiting approval surfaces a clean plan
        # card + approval prompt — NOT the planner's raw spawn/check tool JSON,
        # which would otherwise bury the plan. Skip the per-step tool dump in
        # that case (#159); ordinary runs render their results as usual.
        if self._maybe_emit_pending_plan(mode):
            return task_queue
        self._emit_results(task_queue)
        return task_queue

    def _maybe_emit_pending_plan(self, mode: str) -> bool:
        """Emit the proposed-plan card and resolve the approval decision.

        Renders ``plan.md`` through the ``plan_proposal`` renderer (a bordered
        "Proposed plan" card), then — when a plan-approval resolver is wired (the
        App) — opens the modal and acts on the choice: **approve** drives the
        act-mode run that implements the plan; **reject** abandons it;
        **refine** leaves it pending for the user's feedback. Returns ``True``
        whenever a plan was surfaced so the caller suppresses the raw planner
        tool JSON. Without a resolver (foundation / no-TTY) the plan stays
        pending so the plain-fallback ``/continue`` can approve it.
        """
        if mode != "plan":
            return False
        try:
            pending, revision, plan_path = self.runtime._has_pending_plan_proposal(
                self.state.session_id
            )
        except Exception:  # noqa: BLE001 — defensive around a private probe
            return False
        if not pending:
            return False
        content = "(could not read plan file)"
        try:
            with open(plan_path, encoding="utf-8") as handle:
                content = handle.read()
        except OSError:
            pass
        self._emit(TranscriptItem("plan_proposal", {"text": content, "revision": revision}))

        if self._plan_approval_resolver is None:
            self._emit(
                TranscriptItem(
                    "notice",
                    {"text": "Plan proposed — /continue to approve, or send feedback to refine."},
                )
            )
            return True

        decision = "refine"
        try:
            decision = self._plan_approval_resolver(content, revision or 0) or "refine"
        except Exception:  # noqa: BLE001 — a modal failure must never break the turn
            decision = "refine"

        if decision == "approve":
            self._approve_pending_plan()
        elif decision == "reject":
            self._reject_pending_plan()
        else:
            self._emit(
                TranscriptItem(
                    "notice",
                    {"text": "Kept planning — send your refinement and I will revise the plan."},
                )
            )
        return True

    def _approve_pending_plan(self) -> None:
        """Approve the pending plan, flip plan→act, and execute it.

        Mirrors the Rich-fallback approval (``cli_master``) and the API
        ``/plan/approve`` endpoint: :meth:`SessionRuntime.approve_plan` records
        ``plan_approved`` + the mode transition, then a follow-up act-mode run
        implements the plan with the App's permission modal + lifecycle hooks
        and full transcript fidelity (tool cards via :meth:`_emit_results`).
        """
        if not self.runtime.approve_plan(self.state.session_id):
            self._emit(
                TranscriptItem(
                    "notice",
                    {"text": "Could not approve the plan (a run may still be active)."},
                )
            )
            return
        self.state.mode = "act"
        self._emit(TranscriptItem("notice", {"text": "✓ Plan approved — executing in act mode…"}))
        task_queue = self.runtime.run_sync(
            user_query=PLAN_APPROVE_CONTINUATION,
            model_name=self.state.model_name,
            fallback_models=self.state.fallback_models,
            max_iters=self._max_iters,
            session_id=self.state.session_id,
            tool_registry=self.tool_registry,
            approval_callback=self.permission,
            hook_manager=self._hook_factory() or HookManager(),
            mode="act",
            session_step_budget=self._session_step_budget,
            source_platform="cli",
        )
        self._emit_results(task_queue)

    def _reject_pending_plan(self) -> None:
        """Reject the pending plan; the session stays dormant for refinement."""
        self.runtime.reject_plan(self.state.session_id)
        self._emit(
            TranscriptItem(
                "notice",
                {"text": "✗ Plan rejected — send new instructions or /mode act to proceed."},
            )
        )

    def _emit_results(self, task_queue: Any) -> None:
        """Materialise the turn into the transcript, then replay notices.

        In ``live`` mode (the App) the tool cards + assistant block already
        streamed in-order via the :class:`AgentTranscriptHub`, so the batch dump
        is suppressed (emitting it would duplicate + re-order, bucketed-by-type,
        what already rendered). The plain-REPL path keeps the dump. The
        resilience/recovery notices replay either way.
        """
        if not self._live:
            specs = {spec.tool_id: spec for spec in self.tool_registry.list_specs()}
            for step in task_queue.action_steps:
                spec = specs.get(step.tool_id)
                self._emit(TranscriptItem("tool", self._tool_payload(step, spec)))
            if task_queue.task_result:
                self._emit(TranscriptItem("assistant", {"text": task_queue.task_result}))

        # Decorative post-run notices reuse the shared cli_notices helpers via a
        # recording console, captured into one transcript renderable. The usage
        # footer is intentionally omitted — the status line + sidebar gauge
        # already surface model + tokens + context %; only resilience/recovery
        # notices flow into the transcript here.
        console = self._console_factory()
        halt = print_resilience_events(console, self.store, self.state.session_id)
        maybe_print_recovery_hint(
            console, self.store, self.state.session_id, halt_printed=halt
        )
        self._flush(console)

    # -- tool payload extraction -----------------------------------------

    @staticmethod
    def _tool_input_get(tool_input: Any, keys: tuple[str, ...]) -> str | None:
        """Return the first matching key's value as a string, or ``None``.

        ``ToolInput`` is ``str | dict`` in practice but may also arrive as a
        pydantic model; handle dict access and attribute access defensively and
        never raise.
        """
        for key in keys:
            value: Any = None
            try:
                if isinstance(tool_input, dict):
                    value = tool_input.get(key)
                else:
                    value = getattr(tool_input, key, None)
            except Exception:  # noqa: BLE001 — extraction must never raise
                value = None
            if value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            if text:
                return text
        return None

    @classmethod
    def _args_summary(cls, tool_input: Any) -> str | None:
        """Build a compact ``key=value, …`` header summary, skipping blobs."""
        items: list[tuple[str, Any]] = []
        try:
            if isinstance(tool_input, str):
                text = tool_input.strip()
                return (text[:_ARGS_SUMMARY_MAX] + "…") if len(text) > _ARGS_SUMMARY_MAX else (
                    text or None
                )
            if isinstance(tool_input, dict):
                items = list(tool_input.items())
            else:
                dumped = getattr(tool_input, "model_dump", None)
                if callable(dumped):
                    data = dumped()
                    if isinstance(data, dict):
                        items = list(data.items())
        except Exception:  # noqa: BLE001 — summary is best-effort
            return None
        parts: list[str] = []
        for key, value in items:
            if key in _BLOB_KEYS or value is None:
                continue
            text = value if isinstance(value, str) else str(value)
            if not text:
                continue
            if len(text) > _ARGS_SUMMARY_MAX:
                text = text[:_ARGS_SUMMARY_MAX] + "…"
            parts.append(f"{key}={text}")
            if len(parts) >= 3:
                break
        return ", ".join(parts) if parts else None

    @classmethod
    def _tool_payload(cls, step: Any, spec: Any) -> dict[str, Any]:
        """Build the per-step ``tool`` TranscriptItem payload (pure, defensive).

        Thin shim over :func:`build_tool_payload` (the shared extractor reused by
        the live :class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub`)
        so the batch (plain-REPL) path and the event-driven path build byte-equal
        tool cards from one place.
        """
        result = None
        if getattr(step, "result", None) is not None:
            result = getattr(step.result, "content", step.result)
        is_mcp = spec is not None and getattr(spec, "kind", "") == "mcp"
        return build_tool_payload(
            tool_id=getattr(step, "tool_id", "") or "",
            operation=getattr(step, "operation", None),
            tool_input=getattr(step, "tool_input", None),
            result=result,
            is_mcp=is_mcp,
        )

    # -- helpers ----------------------------------------------------------

    def _flush(self, console: Console) -> None:
        """Forward a recording console's output to the transcript, if any."""
        captured = console.export_text(styles=True)
        if captured.strip():
            self._emit_renderable(Text.from_ansi(captured))


def build_tool_payload(
    *,
    tool_id: str,
    operation: str | None,
    tool_input: Any,
    result: Any,
    is_mcp: bool = False,
) -> dict[str, Any]:
    """Build a ``tool`` TranscriptItem payload (pure, defensive, never raises).

    Includes only the keys that apply for the tool type so the transcript
    renderer can draw a proper per-type card (bash command / file diff /
    generic). Shared by :meth:`TurnEngine._tool_payload` (batch dump) and the
    event-driven :class:`~mewbo_cli.tui.agent_transcript_hub.AgentTranscriptHub`
    so both surfaces render identical cards.
    """
    get = TurnEngine._tool_input_get
    tool_id = tool_id or ""
    payload: dict[str, Any] = {
        "tool_id": tool_id,
        "operation": operation,
        "result": result,
        "is_mcp": is_mcp,
    }

    base_id = tool_id.removesuffix("_tool")
    lower_id = tool_id.lower()
    is_bash = (
        tool_id in _BASH_TOOL_IDS
        or base_id in _BASH_TOOL_IDS
        or any(hint in lower_id for hint in _BASH_TOOL_HINTS)
    )
    is_edit = base_id in {"edit", "write", "read", "str_replace", "create"} or tool_id in {
        "edit",
        "write",
        "read",
    }

    if is_bash:
        command = get(tool_input, _BASH_COMMAND_KEYS)
        if command is None and isinstance(tool_input, str):
            command = tool_input
        if command is not None:
            payload["command"] = command
    else:
        file_path = get(tool_input, _FILE_PATH_KEYS)
        if file_path is not None:
            payload["file_path"] = file_path
        if is_edit:
            old_text = get(tool_input, _OLD_TEXT_KEYS)
            new_text = get(tool_input, _NEW_TEXT_KEYS)
            if new_text is None:
                # A write/create supplies the full file content as new_text.
                content = get(tool_input, _CONTENT_KEYS)
                if content is not None:
                    new_text = content
                    if old_text is None:
                        old_text = ""
            if old_text is not None or new_text is not None:
                payload["old_text"] = old_text or ""
                payload["new_text"] = new_text or ""

    summary = TurnEngine._args_summary(tool_input)
    if summary is not None:
        payload["args_summary"] = summary
    return payload


__all__ = ["CommandRegistryLike", "SkillRegistryLike", "TurnEngine", "build_tool_payload"]
