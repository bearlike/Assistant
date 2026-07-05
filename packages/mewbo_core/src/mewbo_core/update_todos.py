#!/usr/bin/env python3
"""Authoritative live todo list: the terminal-free ``update_todos`` SessionTool.

The engine has no first-class "what am I working on right now" signal — a plan is
a status-less markdown blob and any live progress was a CLI-only heuristic that
re-projected every raw tool call. This module adds ONE authoritative surface: the
agent calls ``update_todos`` FREQUENTLY, re-emitting the FULL statused list each
time, and the tool publishes ONE ``todos`` event through the standard
``append_event`` → ``SessionEventBus`` choke-point. Every interface (CLI dock,
console, API) renders that one event.

Design mirrors ``exit_plan_mode.py``: an internal-tool schema injected into
``bind_tools()`` (NOT registered in the ``ToolRegistry``), dispatched inside
``ToolUseLoop._execute_tool_call``. Unlike ``exit_plan_mode`` it is **terminal-
free** — ``should_terminate_run()`` is always ``False`` so the agent keeps working
after each update. Re-emitting the full list every call is what makes the surface
**compaction-resilient**: the latest event is always the whole truth, so a rebuilt
message list never desyncs the displayed progress.

The ``todos`` event carries a ``source`` discriminator on ONE schema:

- ``source="agent"`` — the live working set the agent maintains as it works
  (what ``update_todos`` emits).
- ``source="plan"`` — an approved plan's steps given optional status (the roadmap,
  checked off); a plan-approval seam emits it via :func:`build_todos_event`.

Shared wire contract (a sibling console workstream consumes it verbatim)::

    {
      "type": "todos",
      "payload": {
        "items": [{"label": str, "status": "pending"|"in_progress"|"completed"}],
        "source": "plan" | "agent",
        "agent_id": str | None,
      },
    }
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from mewbo_core.common import MockSpeaker, get_logger, get_mock_speaker
from mewbo_core.types import Event, TodoItemPayload, TodosPayload

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep

logging = get_logger(name="core.update_todos")


# ------------------------------------------------------------------
# Contract constants (the ONE todo vocabulary, shared by every consumer)
# ------------------------------------------------------------------

TODO_PENDING = "pending"
TODO_IN_PROGRESS = "in_progress"
TODO_COMPLETED = "completed"
TODO_STATUSES: tuple[str, ...] = (TODO_PENDING, TODO_IN_PROGRESS, TODO_COMPLETED)
"""The three lifecycle states of a todo item (the wire contract)."""

TODO_SOURCE_PLAN = "plan"
TODO_SOURCE_AGENT = "agent"
TODO_SOURCES: tuple[str, ...] = (TODO_SOURCE_PLAN, TODO_SOURCE_AGENT)
"""Discriminates an approved-plan roadmap from the agent's live working set."""

TODOS_EVENT_TYPE = "todos"
"""The single event type every interface renders for live progress."""

# Bound a stored label so a runaway model can't balloon memory; renderers clip
# again at display width — this is only a sanity cap on the source of truth.
_LABEL_MAX = 200


# ------------------------------------------------------------------
# Pure contract helpers (shared by the tool AND any plan-seed seam)
# ------------------------------------------------------------------


def normalize_todos(raw: object) -> list[TodoItemPayload]:
    """Coerce arbitrary model input into a clean ``[{label, status}]`` list.

    Never raises — a well-meaning-but-sloppy update must not fail a step (the
    agent calls this frequently, so a reask would be costly). Coercions:

    - Non-dict entries and empty/blank labels are dropped.
    - An unknown/missing ``status`` degrades to ``pending``.
    - **Exactly one** ``in_progress`` is enforced: the first wins; any later
      ``in_progress`` is demoted to ``pending`` (never to ``completed`` — it
      hasn't been done).
    - Labels are stripped and bounded to :data:`_LABEL_MAX` chars.
    """
    items: list[TodoItemPayload] = []
    seen_in_progress = False
    if not isinstance(raw, list):
        return items
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        label = str(entry.get("label", "") or "").strip()
        if not label:
            continue
        status = str(entry.get("status", "") or "").strip().lower()
        if status not in TODO_STATUSES:
            status = TODO_PENDING
        if status == TODO_IN_PROGRESS:
            if seen_in_progress:
                status = TODO_PENDING
            else:
                seen_in_progress = True
        item: TodoItemPayload = {"label": label[:_LABEL_MAX], "status": status}
        items.append(item)
    return items


def build_todos_event(
    items: object,
    *,
    source: str,
    agent_id: str | None,
) -> Event:
    """Build the ONE canonical ``todos`` event (the single contract choke-point).

    ``items`` is normalized here so no caller can publish a malformed event —
    the tool path and any plan-approval seam both flow through this. An
    unrecognised ``source`` degrades to ``agent`` (the live-working-set default).
    """
    resolved_source = source if source in TODO_SOURCES else TODO_SOURCE_AGENT
    payload: TodosPayload = {
        "items": normalize_todos(items),
        "source": resolved_source,
        "agent_id": agent_id,
    }
    return {"type": TODOS_EVENT_TYPE, "payload": payload}


# ------------------------------------------------------------------
# Internal tool schema (injected into bind_tools, NOT in ToolRegistry)
# ------------------------------------------------------------------

UPDATE_TODOS_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "update_todos",
        "description": (
            "Record your CURRENT task list so the user sees faithful live "
            "progress. Call this frequently: right after you plan your approach, "
            "each time you start a task, and each time you finish one. ALWAYS "
            "re-send the FULL list every call (not a delta) — the latest call "
            "replaces the displayed list entirely. Keep EXACTLY ONE item "
            "'in_progress' (the task you are actively working on); mark finished "
            "tasks 'completed' and not-yet-started tasks 'pending'. Use short, "
            "imperative labels (e.g. 'Add throughput meter class'). This does not "
            "end your turn — keep working after calling it. Skip it only for "
            "trivial single-step requests."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": (
                        "The FULL, ordered todo list as it stands right now."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "label": {
                                "type": "string",
                                "description": "Short imperative task label.",
                            },
                            "status": {
                                "type": "string",
                                "enum": list(TODO_STATUSES),
                                "description": (
                                    "'pending' (not started), 'in_progress' "
                                    "(exactly one — the active task), or "
                                    "'completed' (done)."
                                ),
                            },
                        },
                        "required": ["label", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    },
}


# ------------------------------------------------------------------
# Handler (wired into ToolUseLoop like ExitPlanModeTool)
# ------------------------------------------------------------------


class UpdateTodosTool:
    """Handles ``update_todos`` calls — publishes ONE authoritative ``todos`` event.

    One instance per root ``ToolUseLoop`` (attached inline like
    ``ExitPlanModeTool``, so it can be handed the root ``agent_id``). Terminal-
    free: :meth:`should_terminate_run` is always ``False`` — the agent keeps
    working. Class-level ``tool_id`` / ``schema`` / ``modes`` satisfy the
    :class:`~mewbo_core.session_tools.SessionTool` Protocol; ``modes`` restricts
    it to act mode (plan mode drafts a plan via ``exit_plan_mode`` instead).
    """

    tool_id: str = "update_todos"
    schema: dict[str, object] = UPDATE_TODOS_SCHEMA
    modes: frozenset[str] = frozenset({"act"})

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Bind the session id, event logger, and the emitting agent's id.

        Args:
            session_id: Session identifier (parity with other session tools).
            event_logger: Callback for emitting the ``todos`` event; usually
                ``agent_context.event_logger``.
            agent_id: The id stamped on the event so a consumer can attribute
                the list to the right agent (the root, in practice).
        """
        self._session_id = session_id
        self._event_logger = event_logger
        self._agent_id = agent_id

    def should_terminate_run(self) -> bool:
        """Never terminates — the agent keeps working after each update."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Publish the FULL statused list as ONE ``todos`` event (source=agent)."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        items = normalize_todos(args.get("todos"))
        self._emit(
            build_todos_event(items, source=TODO_SOURCE_AGENT, agent_id=self._agent_id)
        )
        done = sum(1 for it in items if it["status"] == TODO_COMPLETED)
        current = next(
            (it["label"] for it in items if it["status"] == TODO_IN_PROGRESS), None
        )
        summary = f"Recorded {len(items)} todo(s) — {done} completed"
        if current:
            summary += f"; now working on: {current}"
        return get_mock_speaker()(content=summary)

    def _emit(self, event: Event) -> None:
        if self._event_logger is not None:
            try:
                self._event_logger(event)
            except Exception as exc:  # noqa: BLE001 — a render event must never break a run
                logging.warning("Failed to emit todos event: {}", exc)


__all__ = [
    "TODOS_EVENT_TYPE",
    "TODO_COMPLETED",
    "TODO_IN_PROGRESS",
    "TODO_PENDING",
    "TODO_SOURCES",
    "TODO_SOURCE_AGENT",
    "TODO_SOURCE_PLAN",
    "TODO_STATUSES",
    "UPDATE_TODOS_SCHEMA",
    "UpdateTodosTool",
    "build_todos_event",
    "normalize_todos",
]
