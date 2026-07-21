#!/usr/bin/env python3
"""Seed-bundle domain models — data-owned, behavior-carrying Pydantic classes.

A :class:`SeedBundle` is the deterministic description of a demo database
state: a handful of *finished* sessions plus armed triggers, authored as
OFFSETS from a frozen ``T0`` so re-seeding is byte-identical (an absolute
wall-clock time baked into the fixture would drift the rendered "45m ago"
labels on every run). Every session-event *kind* is a member of a
discriminated union that owns the method producing its exact store-level
event document — there is deliberately no ``if kind == "..."`` dispatch in
the seeder, mirroring ``mewbo_core.triggers.spec``. Models never import
I/O: the rebased wall-clock instant arrives as a method argument (the seeder
holds the clock).

The bundle JSON is a trust boundary, so every model is ``extra="forbid"``
and validates AT DEFINITION — an unknown field, a malformed trigger spec, or
a session with no terminal completion fails at load, not at seed time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from mewbo_core.triggers.spec import TriggerSpec, parse_trigger
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# JSON value permitted inside a trigger's kind-specific ``fields`` map. The
# concrete ``TriggerSpec`` subclass (``extra="forbid"``) is what actually
# validates those fields; this stays loose so the discriminated union owns the
# per-kind contract (DRY — no second copy of the trigger schema here).
JsonValue = str | int | float | bool | None | list[object] | dict[str, object]

# A fixed reference instant used ONLY to validate a trigger's kind-specific
# fields at bundle-load. ``created_at`` rebasing never affects kind validation,
# so any aware instant works for the fail-fast trial parse.
_VALIDATION_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

TodoStatus = Literal["pending", "in_progress", "completed"]

# Snake_case wire type strings the console timeline (``utils/timeline.ts``) and
# log builder (``utils/logs.ts``) actually key on — kept as constants so a
# member's ``to_event`` never hand-types the string twice.
_TOOL_RESULT = "tool_result"


# ---------------------------------------------------------------------------
# Session events — a discriminated union of the kinds the console renders
# ---------------------------------------------------------------------------


class _SeedEvent(BaseModel):
    """Shared fields + contract for every session-event kind.

    Not itself a union member — concrete kinds declare their own ``kind``
    Literal (see :data:`SeedEventUnion`). ``at_seconds`` is the event's offset
    from its session's start; the seeder rebases it against the frozen ``T0``
    and hands the concrete kind the resulting aware datetime.
    """

    model_config = ConfigDict(extra="forbid")

    at_seconds: int = Field(ge=0, description="Seconds after the session's start instant.")

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Produce the exact store-level event document for this kind.

        Returns a dict carrying its own ``ts`` (the rebased instant); the store's
        ``append_event`` treats a supplied ``ts`` as authoritative
        (``EventRecord = {"ts": now, **event}`` — the spread wins), which is what
        lets a seeded transcript carry historical timestamps.

        ``session_id`` is part of the shared keyword contract so a kind whose wire
        payload embeds it (``widget_ready`` is the only one today) can build a
        faithful document; every other kind ignores it.
        """
        raise NotImplementedError  # pragma: no cover - abstract base


class ContextEvent(_SeedEvent):
    """The per-turn ``context`` event — drives the console turn's model badge.

    Real transcripts write one ``context`` event immediately before each user
    message; the console reads ``payload.model`` from it. Kept deliberately
    minimal (no ``source_platform``/mobile markers) so the session classifies as
    ``origin: user`` and shows in the landing page's default filter.
    """

    kind: Literal["context"] = "context"
    capabilities: list[str] | None = None

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "context", "payload": {"model", "client_capabilities"?}}``."""
        payload: dict[str, object] = {"model": session_model}
        if self.capabilities is not None:
            payload["client_capabilities"] = self.capabilities
        return {"type": "context", "payload": payload, "ts": ts.isoformat()}


class UserEvent(_SeedEvent):
    """A user message — opens a turn in the console timeline."""

    kind: Literal["user"] = "user"
    text: str

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "user", "payload": {"text"}}``."""
        return {"type": "user", "payload": {"text": self.text}, "ts": ts.isoformat()}


class AgentMessageEvent(_SeedEvent):
    """Intermediate root-agent narration (reasoning) — a mid-turn ``agent_message``.

    This is how a real agent's reasoning between tool calls is persisted: an
    ``agent_message`` at ``depth == 0``, rendered in the workspace log lane. It
    must NOT be an ``assistant`` event — that would close the turn and orphan the
    tool steps that follow it.
    """

    kind: Literal["agent_message"] = "agent_message"
    text: str

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "agent_message", "payload": {"text", "agent_id", "depth": 0}}``."""
        return {
            "type": "agent_message",
            "payload": {"text": self.text, "agent_id": agent_id, "depth": 0},
            "ts": ts.isoformat(),
        }


class AssistantEvent(_SeedEvent):
    """The final assistant reply for a turn — closes the turn (renders the bubble)."""

    kind: Literal["assistant"] = "assistant"
    text: str

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "assistant", "payload": {"text"}}``."""
        return {"type": "assistant", "payload": {"text": self.text}, "ts": ts.isoformat()}


class ShellToolEvent(_SeedEvent):
    """A shell tool step — a ``tool_result`` whose ``result`` is a ``kind: shell`` doc.

    The console's ``parseStructuredResult`` renders a ``TerminalCard`` from the
    structured shell JSON regardless of ``tool_id``; the shape mirrors
    ``mewbo_tools.integration.aider_shell_tool`` exactly.
    """

    kind: Literal["shell"] = "shell"
    command: str
    cwd: str | None = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit a ``tool_result`` carrying a ``kind: "shell"`` structured result."""
        result = json.dumps(
            {
                "kind": "shell",
                "command": self.command,
                "cwd": self.cwd,
                "exit_code": self.exit_code,
                "stdout": self.stdout,
                "stderr": self.stderr,
                "duration_ms": self.duration_ms,
            }
        )
        tool_input: dict[str, object] = {"command": self.command}
        if self.cwd:
            tool_input["cwd"] = self.cwd
        payload: dict[str, object] = {
            "tool_id": "shell",
            "operation": "run",
            "tool_input": tool_input,
            "result": result,
            "success": self.exit_code == 0,
            "summary": (self.stdout or self.stderr or "")[:2000],
            "agent_id": agent_id,
            "model": session_model,
        }
        return {"type": _TOOL_RESULT, "payload": payload, "ts": ts.isoformat()}


class FileReadToolEvent(_SeedEvent):
    """A file-read tool step — a ``tool_result`` whose ``result`` is a ``kind: file`` doc.

    Renders as the console's ``FileReadCard``. Mirrors
    ``mewbo_tools.integration.aider_file_tools`` (numbered ``text`` + ``total_lines``).
    """

    kind: Literal["file_read"] = "file_read"
    path: str
    text: str
    total_lines: int | None = None

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit a ``tool_result`` carrying a ``kind: "file"`` structured result."""
        result = json.dumps(
            {
                "kind": "file",
                "path": self.path,
                "text": self.text,
                "total_lines": (
                    self.total_lines
                    if self.total_lines is not None
                    else len(self.text.splitlines())
                ),
            }
        )
        payload: dict[str, object] = {
            "tool_id": "read_file",
            "operation": "read",
            "tool_input": {"file_path": self.path},
            "result": result,
            "success": True,
            "summary": f"Read {self.path}",
            "agent_id": agent_id,
            "model": session_model,
        }
        return {"type": _TOOL_RESULT, "payload": payload, "ts": ts.isoformat()}


class FileEditToolEvent(_SeedEvent):
    """A file-edit tool step — a ``tool_result`` whose ``result`` is a ``kind: diff`` doc.

    Renders as the console's ``DiffCard``. Mirrors
    ``mewbo_tools.integration.edit_common.format_diff_result``
    (``{kind:"diff", title, text, files}``); ``diff`` is a unified diff (the
    ``difflib.unified_diff`` shape the real edit tools emit).
    """

    kind: Literal["file_edit"] = "file_edit"
    path: str
    diff: str
    title: str | None = None

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit a ``tool_result`` carrying a ``kind: "diff"`` structured result."""
        result = json.dumps(
            {
                "kind": "diff",
                "title": self.title or self.path,
                "text": self.diff,
                "files": [self.path],
            }
        )
        payload: dict[str, object] = {
            "tool_id": "search_replace_block",
            "operation": "edit",
            "tool_input": {"file_path": self.path},
            "result": result,
            "success": True,
            "summary": f"Edited {self.path}",
            "agent_id": agent_id,
            "model": session_model,
        }
        return {"type": _TOOL_RESULT, "payload": payload, "ts": ts.isoformat()}


class TodoItem(BaseModel):
    """One authoritative todo row: a label plus its lifecycle status."""

    model_config = ConfigDict(extra="forbid")

    label: str
    status: TodoStatus = "completed"


class TodosEvent(_SeedEvent):
    """An authoritative live checklist — the ``todos`` event schema.

    A finished session's checklist is normally all ``completed``; the console's
    ``parseTodos`` upserts ONE card per turn from ``{items, source, agent_id}``.
    """

    kind: Literal["todos"] = "todos"
    items: list[TodoItem]
    source: Literal["agent", "plan"] = "agent"

    @field_validator("items")
    @classmethod
    def _non_empty(cls, value: list[TodoItem]) -> list[TodoItem]:
        if not value:
            raise ValueError("a todos event must carry at least one item")
        return value

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "todos", "payload": {"items", "source", "agent_id"}}``."""
        payload: dict[str, object] = {
            "items": [{"label": it.label, "status": it.status} for it in self.items],
            "source": self.source,
            "agent_id": agent_id,
        }
        return {"type": "todos", "payload": payload, "ts": ts.isoformat()}


class PlanProposedEvent(_SeedEvent):
    """A plan awaiting the operator's approval — the ``plan_proposed`` event.

    The console maps this to a ``role: "plan"`` timeline entry whose status
    starts ``pending`` ("Awaiting approval"); a later :class:`PlanDecisionEvent`
    with the SAME ``revision`` folds it to approved/rejected. A proposal with no
    matching decision therefore renders as the live awaiting-approval card,
    which is exactly what the plan-approval shot captures.
    """

    kind: Literal["plan_proposed"] = "plan_proposed"
    revision: int = Field(ge=1, description="1-based plan revision; the fold key.")
    content: str = Field(description="Markdown plan body rendered inside the card.")
    summary: str | None = None
    plan_path: str | None = None

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "plan_proposed", "payload": {revision, content, …}}``."""
        payload: dict[str, object] = {
            "revision": self.revision,
            "content": self.content,
        }
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.plan_path is not None:
            payload["plan_path"] = self.plan_path
        return {"type": "plan_proposed", "payload": payload, "ts": ts.isoformat()}


class PlanDecisionEvent(_SeedEvent):
    """The operator's verdict on a proposed plan — ``plan_approved``/``plan_rejected``.

    ``revision`` MUST match an earlier :class:`PlanProposedEvent` in the same
    session: the console folds the decision onto the pending card by scanning
    BACKWARDS for a plan entry with an equal revision, so a mismatched revision
    silently leaves the card pending (validated at bundle load by
    :meth:`SeedSession._plan_decisions_resolve`).
    """

    kind: Literal["plan_decision"] = "plan_decision"
    revision: int = Field(ge=1, description="Revision of the plan being decided.")
    decision: Literal["approved", "rejected"]

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``plan_approved`` or ``plan_rejected`` carrying the fold key."""
        return {
            "type": f"plan_{self.decision}",
            "payload": {"revision": self.revision},
            "ts": ts.isoformat(),
        }


class CompletionEvent(_SeedEvent):
    """The run's terminal ``completion`` — what makes the session read ``completed``.

    ``summarize_session`` derives ``status == "completed"`` from a completion
    event with ``done == true``; a session needs exactly one to render as a
    finished run (enforced by :meth:`SeedSession._has_completion`).
    """

    kind: Literal["completion"] = "completion"
    done_reason: str = "completed"
    task_result: str

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit a ``completion`` event with ``done: true`` + ``done_reason`` + ``task_result``."""
        payload: dict[str, object] = {
            "done": True,
            "done_reason": self.done_reason,
            "task_result": self.task_result,
        }
        return {"type": "completion", "payload": payload, "ts": ts.isoformat()}


class WidgetReadyEvent(_SeedEvent):
    """A rendered stlite widget — the ``widget_ready`` event the console shows inline.

    The FROZEN wire payload
    (``mewbo_core.builtin_plugins.widget_builder.submit_widget.WidgetReadyPayload``,
    mirrored at ``apps/mewbo_console/src/types.ts``) inlines the widget's two files as
    strings, so a seeded event is entirely self-contained: the console maps a
    ``widget_ready`` event to a ``role: "widget"`` timeline entry
    (``utils/timeline.ts``) and boots the stlite kernel straight from ``files`` with
    no server fetch. ``files`` MUST have exactly the keys ``app.py`` and ``data.json``;
    ``data_json`` is a JSON *string* (the widget reads it with
    ``json.load(open("data.json"))``). Keep ``requirements`` empty unless the package
    is in the console's vendored pyodide lockfile — an un-vendored name falls through
    to PyPI and fails on the offline demo network.
    """

    kind: Literal["widget_ready"] = "widget_ready"
    widget_id: str
    app_py: str
    data_json: str = "{}"
    requirements: list[str] = Field(default_factory=list)
    summary: str = ""

    @field_validator("data_json")
    @classmethod
    def _valid_json(cls, value: str) -> str:
        """Fail fast at bundle-load if the inlined ``data.json`` is not valid JSON."""
        json.loads(value)
        return value

    def to_event(
        self, ts: datetime, *, session_model: str, agent_id: str, session_id: str
    ) -> dict[str, object]:
        """Emit ``{"type": "widget_ready", "payload": WidgetReadyPayload}`` (files inlined)."""
        payload: dict[str, object] = {
            "widget_id": self.widget_id,
            "session_id": session_id,
            "files": {"app.py": self.app_py, "data.json": self.data_json},
            "requirements": self.requirements,
            "summary": self.summary,
        }
        return {"type": "widget_ready", "payload": payload, "ts": ts.isoformat()}


SeedEventUnion = Annotated[
    ContextEvent
    | UserEvent
    | AgentMessageEvent
    | AssistantEvent
    | ShellToolEvent
    | FileReadToolEvent
    | FileEditToolEvent
    | TodosEvent
    | WidgetReadyEvent
    | PlanProposedEvent
    | PlanDecisionEvent
    | CompletionEvent,
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


class SeedSession(BaseModel):
    """One finished session: identity, model, a T0-relative start, and its events."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    model: str
    offset_seconds: int = Field(
        description="Session start relative to T0, in seconds (negative = in the past).",
    )
    summary: str | None = None
    events: list[SeedEventUnion]

    @field_validator("events")
    @classmethod
    def _strictly_increasing(cls, value: list[SeedEventUnion]) -> list[SeedEventUnion]:
        """Every event must have a strictly larger offset than the previous one.

        The rebased ``ts`` is ``T0 + offset_seconds + at_seconds`` at whole-second
        granularity, and the transcript is sorted by ``ts``. Equal offsets would
        collide onto one instant and make the render order non-deterministic — the
        exact staleness hazard T0-offsets exist to avoid.
        """
        if not value:
            raise ValueError("a session must have at least one event")
        prev = -1
        for event in value:
            if event.at_seconds <= prev:
                raise ValueError(
                    "event at_seconds must be strictly increasing within a session "
                    f"(got {event.at_seconds} after {prev})"
                )
            prev = event.at_seconds
        return value

    @model_validator(mode="after")
    def _plan_decisions_resolve(self) -> SeedSession:
        """Every plan decision must fold onto an EARLIER proposal of the same revision.

        The console folds a decision backwards onto the matching pending card and
        silently does nothing when no revision matches — the card would just stay
        "Awaiting approval". That is a wrong screenshot with a green test, so the
        mismatch is caught here at bundle load instead.
        """
        proposed: set[int] = set()
        for event in self.events:
            if isinstance(event, PlanProposedEvent):
                if event.revision in proposed:
                    raise ValueError(
                        f"session {self.id!r} proposes plan revision "
                        f"{event.revision} more than once"
                    )
                proposed.add(event.revision)
            elif isinstance(event, PlanDecisionEvent) and event.revision not in proposed:
                raise ValueError(
                    f"session {self.id!r} decides plan revision {event.revision} "
                    "before (or without) proposing it"
                )
        return self

    @model_validator(mode="after")
    def _has_completion(self) -> SeedSession:
        """A demo session must end in a completion event to read as a finished run."""
        if not any(isinstance(e, CompletionEvent) for e in self.events):
            raise ValueError(
                f"session {self.id!r} must include a completion event so the console "
                "renders it as a completed run"
            )
        return self


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class SeedTrigger(BaseModel):
    """A durable reverse-invocation trigger, authored against a seeded session.

    ``kind`` + ``fields`` are handed straight to ``mewbo_core.triggers.parse_trigger``
    (the ONE parse seam), so the concrete ``TriggerSpec`` subclass owns every
    per-kind rule — this model never re-implements them. The trigger's console
    display name IS its ``wake_prompt`` (``TriggerRow`` renders it as the primary
    line), so set that to the human name the screenshot flows expect.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    session: str = Field(description="A SeedSession.id this trigger is armed against.")
    kind: str
    wake_prompt: str
    created_by: Literal["agent", "user"] = "agent"
    created_at_offset: int = Field(
        default=0, description="Trigger creation relative to T0, in seconds."
    )
    fields: dict[str, JsonValue] = Field(
        default_factory=dict,
        description="Kind-specific TriggerSpec fields (cron expr, webhook secret, …).",
    )

    def to_spec(self, t0: datetime, *, session_id: str) -> TriggerSpec:
        """Build the concrete ``TriggerSpec`` for this trigger against a rebased clock."""
        raw: dict[str, object] = {
            "kind": self.kind,
            "id": self.id,
            "session_id": session_id,
            "wake_prompt": self.wake_prompt,
            "created_by": self.created_by,
            "created_at": (t0 + timedelta(seconds=self.created_at_offset)).isoformat(),
            **self.fields,
        }
        return parse_trigger(raw)

    @model_validator(mode="after")
    def _validate_spec(self) -> SeedTrigger:
        """Fail fast at bundle-load: trial-parse into the concrete kind.

        The ``TriggerSpec`` subclass (``extra="forbid"``) rejects an unknown/typo'd
        field or an invalid cron expression HERE, at definition, rather than
        surfacing deep in the seeder run.
        """
        self.to_spec(_VALIDATION_T0, session_id=self.session or "_validation_")
        return self


class SeedApiKey(BaseModel):
    """One issued API key row for the Security settings pane.

    The key store's own ``create_key`` mints a ``uuid4`` id and stamps
    ``utc_now_iso()`` — both nondeterministic, so a shot of the issued-keys list
    could never be byte-identical. This model therefore carries an EXPLICIT id
    and a T0-relative ``created_at_offset``, and the seeder writes the record
    directly (see ``DemoSeeder._seed_api_key`` for why that one deviation from
    "write through the store contract" is unavoidable here).

    The stored ``key_hash`` is derived from a salt that is never rendered, so a
    seeded key is deliberately UNUSABLE for authentication: the demo stack must
    portray the surface without minting anything that could actually authorize a
    request. ``id`` and ``label`` are invented demo strings — never copy a real
    deployment's key ids or labels in here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable, invented key id — rendered in the pane.")
    label: str = Field(description="Invented human label, e.g. 'laptop-cli'.")
    created_at_offset: int = Field(
        description="Key creation relative to T0, in seconds (negative = in the past).",
    )
    revoked_at_offset: int | None = Field(
        default=None,
        description="Revocation relative to T0; None leaves the key Active.",
    )


class SeedBundle(BaseModel):
    """The whole deterministic demo state: sessions + triggers + issued API keys."""

    model_config = ConfigDict(extra="forbid")

    sessions: list[SeedSession]
    triggers: list[SeedTrigger] = Field(default_factory=list)
    api_keys: list[SeedApiKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_refs(self) -> SeedBundle:
        """Ids are unique and every trigger references a real session."""
        session_ids = [s.id for s in self.sessions]
        if len(set(session_ids)) != len(session_ids):
            raise ValueError("duplicate session id in bundle")
        trigger_ids = [t.id for t in self.triggers]
        if len(set(trigger_ids)) != len(trigger_ids):
            raise ValueError("duplicate trigger id in bundle")
        key_ids = [k.id for k in self.api_keys]
        if len(set(key_ids)) != len(key_ids):
            raise ValueError("duplicate api key id in bundle")
        known = set(session_ids)
        for trigger in self.triggers:
            if trigger.session not in known:
                raise ValueError(
                    f"trigger {trigger.id!r} references unknown session {trigger.session!r}"
                )
        return self


__all__ = [
    "JsonValue",
    "TodoStatus",
    "TodoItem",
    "ContextEvent",
    "UserEvent",
    "AgentMessageEvent",
    "AssistantEvent",
    "ShellToolEvent",
    "FileReadToolEvent",
    "FileEditToolEvent",
    "TodosEvent",
    "WidgetReadyEvent",
    "PlanProposedEvent",
    "PlanDecisionEvent",
    "CompletionEvent",
    "SeedEventUnion",
    "SeedSession",
    "SeedTrigger",
    "SeedApiKey",
    "SeedBundle",
]
