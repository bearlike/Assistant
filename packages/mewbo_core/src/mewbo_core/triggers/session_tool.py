#!/usr/bin/env python3
"""``schedule_trigger`` SessionTool — the agent's own reverse-invocation control.

Lets the LLM arm/list/cancel :class:`~mewbo_core.triggers.spec.TriggerSpec`
records for its OWN session instead of burning turns on a poll loop: arm a
trigger and end the turn, and the orchestrator re-invokes the session later
with the trigger's ``wake_prompt`` when it fires (the actual re-invocation —
the scheduler/webhook-receiving service that watches ``TriggerStoreBase`` and
fires due triggers — is a separate concern, built elsewhere; this tool only
owns the agent-facing admission surface: arm, list, cancel).

Follows the ``update_todos.py`` / ``exit_plan_mode.py`` pattern: a class
satisfying the :class:`~mewbo_core.session_tools.SessionTool` Protocol,
terminal-free (:meth:`should_terminate_run` always ``False`` — arming a
trigger is a normal step, not a plan-mode-style exit). Unlike those two
internal tools it is NOT hand-attached inline at root depth 0 by default — it
is built through the ordinary :class:`~mewbo_core.session_tools.SessionToolRegistry`
plugin path, which feeds a constructor only ``session_id`` + ``event_logger``.
``agent_id`` is therefore an OPTIONAL constructor kwarg (default ``None``,
mirroring :class:`~mewbo_core.update_todos.UpdateTodosTool`'s DI shape
exactly) so a caller that DOES want live agent attribution on the
``trigger_armed`` event can still inline-attach this tool the same way
``UpdateTodosTool`` is attached. There is currently no per-step turn counter
anywhere in the engine (`AgentContext` carries no such field) to source
``TriggerProvenance.step`` from, so it is always stamped ``None`` here — a
future step-counter seam can thread a value through without changing this
tool's contract.

Errors (malformed input, a policy rejection, an unknown ``trigger_id``) all
return the shared ``{"error": {"code", "message"}}`` envelope
(``tool_use_loop.py``'s ``_session_tool_error_envelope`` reclassifies it as a
FAILED step while still handing the model the envelope text) — the SAME
contract the wiki/scg plugin tools use. ``mewbo_core`` sits BELOW
``mewbo_graph`` in the dependency DAG, so this module can't import their
``err_result``/``ok_result`` helpers; ``_err_result``/``_ok_result`` below are
a local mirror of the identical shape.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import (
    DEFAULT_SESSION_TOOL_MODES,
    EventLogger,
    SessionTool,
    SessionToolFactory,
)
from mewbo_core.triggers.policy import TriggerPolicy
from mewbo_core.triggers.spec import TriggerSpec, parse_trigger
from mewbo_core.triggers.store import TriggerStoreBase

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.triggers.session_tool")

# Bound preview lengths so a runaway wake_prompt can't balloon a `list`
# response or the `trigger_armed` event — mirrors update_todos.py's _LABEL_MAX.
_WAKE_PROMPT_PREVIEW_MAX = 160
_SUMMARY_PREVIEW_MAX = 120

# Which ScheduleTriggerArgs fields feed each concrete TriggerSpec kind. Only
# listed fields are pulled into the raw dict handed to parse_trigger — this
# is what keeps an irrelevant field (e.g. `at` supplied for kind="webhook")
# from tripping the concrete model's `extra="forbid"`.
_KIND_FIELDS: dict[str, tuple[str, ...]] = {
    "time.at": ("at",),
    "time.cron": ("cron",),
    "ci.workflow": ("repo", "run_id", "workflow", "ref", "conclusion_filter"),
    "forge.pr": ("repo", "number", "events"),
    "webhook": ("hmac_header",),
}


# ------------------------------------------------------------------
# Args schema (Pydantic model ⇒ OpenAI function schema via pydantic_to_openai_tool)
# ------------------------------------------------------------------


class ScheduleTriggerArgs(BaseModel):
    """Arm, list, or cancel a reverse-invocation trigger for THIS session.

    A trigger is a durable "wake me up" record — a wall-clock alarm, a cron
    schedule, a CI run completing, a forge PR event, or an inbound webhook —
    that re-engages this session LATER. Arm one and END YOUR TURN instead of
    polling in a loop: polling burns turns and tokens waiting for something
    that hasn't happened yet, while an armed trigger costs nothing until it
    actually fires and re-invokes you with your own `wake_prompt`.

    `operation="arm"` schedules a NEW trigger — set `kind` plus that kind's
    fields (`time.at` needs `at`; `time.cron` needs `cron`; `ci.workflow`
    needs `repo` + exactly one of `run_id`/`workflow`; `forge.pr` needs
    `repo` + `number` + `events`; `webhook` needs nothing else — a secret is
    generated for you) and a `wake_prompt` (the instruction you want handed
    back to you when it fires). `operation="list"` shows every trigger you've
    armed on this session (id, kind, status, progress) — check this before
    arming a near-duplicate. `operation="cancel"` retires one by `trigger_id`
    (safe to call again on an already-finished trigger — it just reports its
    current status instead of erroring).
    """

    model_config = ConfigDict(extra="forbid")

    operation: Literal["arm", "list", "cancel"] = Field(
        description=(
            "`arm` schedules a new trigger; `list` shows this session's "
            "triggers; `cancel` retires one by id."
        )
    )

    # -- arm: shared fields --------------------------------------------
    kind: Literal["time.at", "time.cron", "ci.workflow", "forge.pr", "webhook"] | None = (
        Field(
            default=None,
            description=(
                "`arm` only: the trigger kind. Determines which fields below "
                "are required."
            ),
        )
    )
    wake_prompt: str | None = Field(
        default=None,
        description=(
            "`arm` only: the instruction handed back to you when this trigger "
            "fires — write it as if resuming your own train of thought."
        ),
    )
    action: Literal["message", "start"] = Field(
        default="message",
        description=(
            "`arm` only: how the orchestrator re-engages on fire — `message` "
            "injects `wake_prompt` into this session, `start` opens a fresh "
            "turn with it."
        ),
    )
    max_fires: int | None = Field(
        default=None,
        ge=1,
        description=(
            "`arm` only: cap on how many times this trigger may fire before "
            "it auto-completes. Omit for unlimited (subject to the deployment "
            "policy cap — `time.at` is always exactly 1, forced)."
        ),
    )
    expires_at: datetime | None = Field(
        default=None,
        description=(
            "`arm` only: hard deadline after which an unfired trigger is "
            "abandoned. Omit to use the deployment default."
        ),
    )

    # -- arm: kind=time.at -----------------------------------------------
    at: datetime | None = Field(
        default=None, description="`kind=time.at`: the wall-clock instant to fire at."
    )

    # -- arm: kind=time.cron ----------------------------------------------
    cron: str | None = Field(
        default=None, description="`kind=time.cron`: a 5-field cron expression."
    )

    # -- arm: kind=ci.workflow / forge.pr (shared `repo`) ------------------
    repo: str | None = Field(
        default=None,
        description="`kind=ci.workflow`/`forge.pr`: the forge repo as `owner/name`.",
    )
    run_id: int | None = Field(
        default=None,
        description=(
            "`kind=ci.workflow`: fire when THIS specific run completes "
            "(mutually exclusive with `workflow`)."
        ),
    )
    workflow: str | None = Field(
        default=None,
        description=(
            "`kind=ci.workflow`: fire on the next completed run of this "
            "workflow name (mutually exclusive with `run_id`)."
        ),
    )
    ref: str | None = Field(
        default=None, description="`kind=ci.workflow`: restrict to this git ref."
    )
    conclusion_filter: list[str] | None = Field(
        default=None,
        description=(
            '`kind=ci.workflow`: only fire when the conclusion is one of '
            'these (e.g. ["success", "failure"]). Omit to fire on any conclusion.'
        ),
    )

    # -- arm: kind=forge.pr -------------------------------------------------
    number: int | None = Field(
        default=None, description="`kind=forge.pr`: the PR number."
    )
    events: list[Literal["merged", "review", "comment", "ci_status"]] | None = Field(
        default=None,
        description="`kind=forge.pr`: which PR lifecycle events fire this trigger.",
    )

    # -- arm: kind=webhook ----------------------------------------------
    hmac_header: str | None = Field(
        default=None,
        description=(
            "`kind=webhook`: optional header name carrying an HMAC-SHA256 "
            "signature to verify (in addition to the URL secret)."
        ),
    )

    # -- cancel -----------------------------------------------------------
    trigger_id: str | None = Field(
        default=None, description="`cancel` only: the id of the trigger to retire."
    )

    @model_validator(mode="after")
    def _check_operation_fields(self) -> ScheduleTriggerArgs:
        """Require the fields each operation needs (fail closed at the boundary).

        Deliberately does NOT re-validate kind-specific requirements (e.g.
        "time.at requires `at`") — that duplication belongs to `parse_trigger`
        / the concrete `TriggerSpec` subclasses alone (spec.py's own
        docstring: no service-side `if kind == ...` dispatch).
        """
        if self.operation == "arm":
            if self.kind is None:
                raise ValueError("operation=arm requires `kind`")
            if not (self.wake_prompt and self.wake_prompt.strip()):
                raise ValueError("operation=arm requires non-empty `wake_prompt`")
        elif self.operation == "cancel" and not (
            self.trigger_id and self.trigger_id.strip()
        ):
            raise ValueError("operation=cancel requires `trigger_id`")
        return self


# ------------------------------------------------------------------
# The SessionTool
# ------------------------------------------------------------------


class ScheduleTriggerTool:
    """Handles ``schedule_trigger`` calls — arm/list/cancel this session's triggers."""

    tool_id: str = "schedule_trigger"
    schema: dict[str, object] = pydantic_to_openai_tool(
        ScheduleTriggerArgs, name="schedule_trigger"
    )
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        store: TriggerStoreBase,
        policy: TriggerPolicy,
        event_logger: Callable[[Event], None] | None = None,
        agent_id: str | None = None,
    ) -> None:
        """Bind the owning session id + collaborators (store, policy, event sink).

        Args:
            session_id: Session identifier (parity with other session tools).
            store: The trigger persistence backend (`create`/`get`/`list`/`update`).
            policy: Admission limits gate — see `TriggerPolicy.admit`.
            event_logger: Callback for emitting the `trigger_armed` event;
                usually `agent_context.event_logger`.
            agent_id: The id stamped on `TriggerProvenance.agent_id`, when the
                caller wires this tool inline (parity with `UpdateTodosTool`).
                `None` when built through the ordinary plugin registry path,
                which doesn't have an agent id to pass.
        """
        self._session_id = session_id
        self._store = store
        self._policy = policy
        self._event_logger = event_logger
        self._agent_id = agent_id

    def should_terminate_run(self) -> bool:
        """Never terminates — arming/listing/cancelling is a normal step."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); default parity with the Protocol."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Dispatch arm/list/cancel off `tool_input.operation`."""
        try:
            args = ScheduleTriggerArgs.model_validate(action_step.tool_input or {})
        except ValidationError as exc:
            return self._err_result("validation", str(exc))
        if args.operation == "arm":
            return self._arm(args)
        if args.operation == "list":
            return self._list()
        return self._cancel(args)

    # -- operations ---------------------------------------------------------

    def _arm(self, args: ScheduleTriggerArgs) -> MockSpeaker:
        """Build the concrete spec, admit it against policy, persist, emit."""
        if args.kind is None:  # unreachable — guarded by _check_operation_fields
            return self._err_result("validation", "operation=arm requires `kind`")
        try:
            spec = parse_trigger(self._raw_spec(args))
        except ValidationError as exc:
            return self._err_result("validation", str(exc))
        armed_count = len(self._store.list(session_id=self._session_id, status="armed"))
        try:
            spec = self._policy.admit(spec, armed_count)
        except ValueError as exc:
            return self._err_result("policy", str(exc))
        self._store.create(spec)
        self._emit(self._trigger_armed_event(spec))
        now = datetime.now(timezone.utc)
        return self._ok_result(
            {
                "operation": "arm",
                "id": spec.id,
                "kind": spec.kind,
                "status": spec.status,
                "expires_at": self._iso(spec.expires_at),
                "next_fire_at": self._iso(spec.next_fire_at(now)),
            }
        )

    def _raw_spec(self, args: ScheduleTriggerArgs) -> dict[str, object]:
        """Assemble the dict handed to `parse_trigger` for an `arm` call.

        Stamps the parts the agent must never choose itself
        (`session_id`, `created_by="agent"`, `provenance`) and copies only
        the kind-relevant fields off *args* (`_KIND_FIELDS`) so an
        irrelevant field supplied for the wrong kind is silently dropped
        rather than tripping the concrete model's `extra="forbid"`.
        """
        raw: dict[str, object] = {
            "kind": args.kind,
            "session_id": self._session_id,
            "wake_prompt": (args.wake_prompt or "").strip(),
            "action": args.action,
            "created_by": "agent",
            # No per-step turn counter exists anywhere in the engine yet
            # (see the module docstring) — stamped None until one does.
            "provenance": {"agent_id": self._agent_id, "step": None},
        }
        if args.max_fires is not None:
            raw["max_fires"] = args.max_fires
        if args.expires_at is not None:
            raw["expires_at"] = args.expires_at
        for field in _KIND_FIELDS.get(args.kind or "", ()):
            value = getattr(args, field)
            if value is not None:
                raw[field] = value
        return raw

    def _list(self) -> MockSpeaker:
        """This session's triggers, compact — the agent's own visibility surface."""
        now = datetime.now(timezone.utc)
        triggers = self._store.list(session_id=self._session_id)
        items = [self._compact(spec, now) for spec in triggers]
        return self._ok_result({"operation": "list", "count": len(items), "triggers": items})

    @classmethod
    def _compact(cls, spec: TriggerSpec, now: datetime) -> dict[str, object]:
        prompt = spec.wake_prompt.strip()
        if len(prompt) > _WAKE_PROMPT_PREVIEW_MAX:
            prompt = prompt[:_WAKE_PROMPT_PREVIEW_MAX]
        return {
            "id": spec.id,
            "kind": spec.kind,
            "status": spec.status,
            "wake_prompt": prompt,
            "fires": spec.fires,
            "max_fires": spec.max_fires,
            "next_fire_at": cls._iso(spec.next_fire_at(now)),
        }

    def _cancel(self, args: ScheduleTriggerArgs) -> MockSpeaker:
        """Transition a trigger to cancelled — idempotent on an already-terminal one."""
        trigger_id = (args.trigger_id or "").strip()
        trigger = self._store.get(trigger_id)
        if trigger is None or trigger.session_id != self._session_id:
            return self._err_result("not_found", f"no trigger {trigger_id!r} on this session")
        if trigger.is_terminal:
            return self._ok_result(
                {
                    "operation": "cancel",
                    "id": trigger.id,
                    "status": trigger.status,
                    "already_terminal": True,
                }
            )
        trigger.transition("cancelled")
        self._store.update(trigger)
        return self._ok_result(
            {"operation": "cancel", "id": trigger.id, "status": trigger.status}
        )

    def _emit(self, event: Event) -> None:
        if self._event_logger is not None:
            try:
                self._event_logger(event)
            except Exception as exc:  # noqa: BLE001 — a render event must never break a run
                logging.warning("Failed to emit trigger_armed event: {}", exc)

    # -- structured-result envelope helpers (local mirror of the wiki/scg shape) --

    @staticmethod
    def _err_result(code: str, message: str) -> MockSpeaker:
        """Structured-error envelope: ``str({"error": {"code", "message"}})``.

        The envelope SHAPE is the contract (parsed structurally by
        ``tool_use_loop._session_tool_error_envelope``), not this helper — see
        the module docstring for why it's a local copy rather than a shared
        import.
        """
        return MockSpeaker(content=str({"error": {"code": code, "message": message}}))

    @staticmethod
    def _ok_result(payload: dict[str, object]) -> MockSpeaker:
        """Successful structured payload.

        ``str(dict)``, the same Python-repr shape as :meth:`_err_result` (never
        ``json.dumps`` — that would emit JSON ``null``/``true``/``false``, which
        the loop's envelope parser, ``ast.literal_eval``, can't read back).
        """
        return MockSpeaker(content=str(payload))

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        """ISO-8601 string for a wire payload, or ``None`` through unchanged."""
        return value.isoformat() if value is not None else None

    @staticmethod
    def _trigger_armed_event(spec: TriggerSpec) -> Event:
        """Build the ``trigger_armed`` transcript event (compact confirmation)."""
        summary = f"Armed {spec.kind} trigger: {spec.wake_prompt[:_SUMMARY_PREVIEW_MAX]}"
        payload: dict[str, object] = {
            "trigger_id": spec.id,
            "kind": spec.kind,
            "summary": summary,
        }
        return {"type": "trigger_armed", "payload": payload}


# ------------------------------------------------------------------
# Down-only registration seam
# ------------------------------------------------------------------
# ``schedule_trigger`` needs two collaborators the generic ``SessionToolRegistry``
# manifest path can't supply (that path feeds only ``session_id`` +
# ``event_logger``): the durable trigger STORE and the admission POLICY, both
# owned by the app. So the app pushes them ONCE at startup and core builds the
# registry factory closing over them — mirroring ``plugins.register_builtin_root``
# and ``mewbo_api.apps.plugin.runtime.register_app_submitter``. Registering it as
# an ordinary factory (rather than the old root-only ``extra_session_tools``
# injection) is what finally lets a SPAWNED sub-agent hold the tool: children
# already receive the shared ``SessionToolRegistry``, so an agent whose
# ``allowed_tools`` names ``schedule_trigger`` (the app-builder AgentDef) now
# actually binds it. ``None`` until the app pushes ⇒ a deployment without the
# trigger subsystem (CLI, tests) never surfaces the tool — byte-identical to
# before, where only the API's ``extra_session_tools`` seam injected it.

_TRIGGER_TOOL_PROVIDER: tuple[TriggerStoreBase, TriggerPolicy] | None = None


def register_schedule_trigger_provider(
    store: TriggerStoreBase, policy: TriggerPolicy
) -> None:
    """Push the store+policy ``schedule_trigger`` needs; the API calls this once.

    Last write wins (a test swaps a fake cleanly). After this,
    :func:`schedule_trigger_factory` returns a live factory that every
    ``Orchestrator`` registers into its ``SessionToolRegistry``.
    """
    global _TRIGGER_TOOL_PROVIDER  # noqa: PLW0603 - single composition-root handle
    _TRIGGER_TOOL_PROVIDER = (store, policy)


def schedule_trigger_factory() -> SessionToolFactory | None:
    """The ``schedule_trigger`` factory, or ``None`` when the app hasn't pushed.

    Marked ``unconditional`` so it surfaces to any un-scoped session (the
    always-on-at-root shape it had as an ``extra_session_tools`` injection),
    while an explicit ``allowed_tools`` still admits it by id for a scoped
    sub-agent — see :class:`~mewbo_core.session_tools.SessionToolFactory`.
    """
    if _TRIGGER_TOOL_PROVIDER is None:
        return None
    store, policy = _TRIGGER_TOOL_PROVIDER

    def _build(session_id: str, event_logger: EventLogger | None) -> SessionTool:
        return ScheduleTriggerTool(
            session_id=session_id,
            store=store,
            policy=policy,
            event_logger=event_logger,
        )

    return SessionToolFactory(
        tool_id=ScheduleTriggerTool.tool_id,
        build=_build,
        unconditional=True,
    )


__all__ = [
    "ScheduleTriggerArgs",
    "ScheduleTriggerTool",
    "register_schedule_trigger_provider",
    "schedule_trigger_factory",
]
