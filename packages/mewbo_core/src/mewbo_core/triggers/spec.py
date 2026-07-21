#!/usr/bin/env python3
"""Trigger domain model — data-owned behavioral Pydantic classes.

A ``TriggerSpec`` is a durable "wake me up" record: something outside the
current turn (a wall clock, a cron schedule, a CI run, a forge PR event, or
an inbound webhook) that should re-engage a session later. Every trigger
*kind* owns its own due-ness/match logic as a method — there is deliberately
no service-side ``if kind == "..."`` dispatch anywhere; that would drift out
of sync with the model the moment a kind gains a new field. Models never
import I/O clients; anything external (the current time, an inbound
webhook's headers/body, a normalized CI/PR payload) arrives as a method
argument.

Lifecycle mirrors ``AgentHypervisor``'s absorbing-state ethos
(``hypervisor.py`` — see ``AgentStatus``): two *resting* statuses
(``armed``/``paused``) a trigger can move between freely, and four
*terminal* statuses it can only enter, never leave. Firing is a transition
alongside the status graph, not a status itself.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Annotated, ClassVar, Literal

from croniter import croniter  # type: ignore[import-untyped]  # no stubs published
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

TriggerStatus = Literal["armed", "paused", "completed", "failed", "cancelled", "expired"]

_RESTING_STATUSES: frozenset[str] = frozenset({"armed", "paused"})
_TERMINAL_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled", "expired"})


class TriggerProvenance(BaseModel):
    """Who/what asked for this trigger to be armed."""

    model_config = ConfigDict(extra="forbid")

    agent_id: str | None = None
    step: int | None = None


class TriggerAuthority(BaseModel):
    """A snapshot of the arming caller's authority, frozen at arm time.

    A trigger fires LATER, out-of-band, with no live request behind it — so a
    fired trigger must not re-engage its session with ambient full power. The
    app captures the arming principal's identity here (when auth is enabled) so
    the fire path can rebuild the SAME role ceiling the caller had. Plain data
    only — ``subject``/role names/scope strings — so core stays free of any
    identity-kernel import; the app resolves these fields back into a session
    scope at fire time.

    ``scopes`` preserves the three-state law of the identity kernel: ``None`` =
    unrestricted-legacy (impose no narrowing), ``()`` = explicitly scopeless, a
    non-empty tuple = exactly those. ``None`` and ``()`` are NEVER collapsed —
    the fail-open trap a scopeless key becoming unrestricted.
    """

    model_config = ConfigDict(extra="forbid")

    # Mirrors ``mewbo_iam.principal.SUBJECT_PREFIXES``. Deliberately a second
    # copy rather than an import: this module stays free of any identity-kernel
    # dependency (see the class docstring), and core must not import up into a
    # library that already imports core. Change one, change the other.
    _SUBJECT_PREFIXES: ClassVar[tuple[str, ...]] = ("user:", "svc:")

    principal_subject: str
    roles: tuple[str, ...] = ()
    scopes: tuple[str, ...] | None = None

    @field_validator("principal_subject")
    @classmethod
    def _require_known_prefix(cls, value: str) -> str:
        """Require the same ``user:``/``svc:`` shape a live principal carries.

        This field is written from a ``Principal.subject`` and read back into
        one at fire time, so validating only the live side would let a record
        persisted by an older writer fail at the far end of the round-trip —
        when a trigger fires, far from whatever produced it. Checking both ends
        keeps the malformed value out at the edge it entered.

        The fire path infers ``kind`` from this prefix, so an unrecognized one
        does not fail there: it silently rebuilds the caller as a *service*
        principal.
        """
        prefix = next((p for p in cls._SUBJECT_PREFIXES if value.startswith(p)), None)
        if prefix is None or not value[len(prefix) :].strip():
            expected = " or ".join(f"{p}<id>" for p in cls._SUBJECT_PREFIXES)
            raise ValueError(f"principal_subject must be {expected}, got {value!r}")
        return value


class TriggerSpec(BaseModel):
    """Shared fields + lifecycle behavior for every trigger kind.

    Not itself a union member — concrete kinds declare their own ``kind``
    Literal (see :data:`TriggerUnion`). Instantiating ``TriggerSpec`` directly
    fails validation (``kind`` is required, untyped) by design: it exists to
    hold the common contract, not to be armed on its own.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    session_id: str
    wake_prompt: str
    action: Literal["message", "start"] = "message"
    status: TriggerStatus = "armed"
    fires: int = 0
    max_fires: int | None = None
    expires_at: datetime | None = None
    # ``created_at`` always gets its aware default (never agent-authored — the
    # SessionTool schema doesn't expose it) and ``last_fired_at`` is only ever
    # written by ``record_fire(now)`` with the caller's own aware ``now`` — so
    # neither needs the naive-rejection guard applied to ``at``/``expires_at``,
    # which arrive as agent-authored ISO strings.
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    created_by: Literal["agent", "user"]
    provenance: TriggerProvenance = Field(default_factory=TriggerProvenance)
    # The arming caller's authority, snapshotted at arm time (app-set, and only
    # when auth is enabled). ``None`` — every trigger armed before IAM, and every
    # auth-disabled deployment — means "no captured authority": the fire path
    # falls back to today's ambient re-engage, byte-identical. Lives on the shared
    # base (the stored RECORD contract), never on a per-kind variant, so the
    # discriminated union stays pure and every kind carries it for free.
    authority: TriggerAuthority | None = None
    last_fired_at: datetime | None = None
    last_error: str | None = None

    # -- validation helpers (shared by subclass field_validators via cls) ---

    @staticmethod
    def _validate_owner_repo(value: str) -> str:
        """Validate an ``owner/name`` forge repo identifier. Shared by ci/forge kinds."""
        parts = value.split("/")
        if len(parts) != 2 or not all(parts):
            raise ValueError(f"repo must be 'owner/name', got {value!r}")
        return value

    @staticmethod
    def _require_aware(value: datetime) -> datetime:
        """Reject a tz-naive datetime rather than silently assuming a zone.

        An LLM-authored ISO string with no UTC offset parses naive; comparing it
        against an aware ``now`` later (``is_due``/``is_expired``) raises
        ``TypeError`` deep in the watcher's poll loop. Rejecting here — instead of
        stamping UTC — surfaces a validation error back to the agent immediately
        (self-correctable) rather than silently shifting the author's intended
        instant. Shared by every field that accepts an agent-authored datetime.
        """
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "datetime must include a timezone offset (e.g. Z or +05:30), "
                f"got {value.isoformat()!r}"
            )
        return value

    @field_validator("expires_at")
    @classmethod
    def _validate_expires_at(cls, value: datetime | None) -> datetime | None:
        return cls._require_aware(value) if value is not None else value

    # -- lifecycle ------------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        """Whether this trigger has reached an absorbing status."""
        return self.status in _TERMINAL_STATUSES

    def transition(self, to: TriggerStatus) -> None:
        """Move to status ``to``, guarding illegal transitions.

        A terminal status is absorbing — any transition attempted from one
        raises. Within the resting pair (``armed``/``paused``) any move is
        legal; a resting status may also move directly to any terminal one.
        A no-op transition (``to == self.status``) is a silent success.
        """
        if to == self.status:
            return
        if self.status in _TERMINAL_STATUSES:
            raise ValueError(
                f"trigger {self.id} cannot transition out of terminal status {self.status!r}"
            )
        if to in _RESTING_STATUSES or to in _TERMINAL_STATUSES:
            self.status = to
            return
        raise ValueError(f"illegal trigger transition {self.status!r} -> {to!r}")

    def record_fire(self, now: datetime, error: str | None = None) -> TriggerSpec:
        """Record one firing: bump ``fires``, stamp timestamps, maybe complete.

        Auto-completes (``transition("completed")``) once ``fires`` reaches
        ``max_fires``. An errored fire still counts — the caller (a future
        retry/backoff policy) reads ``last_error`` to decide whether to
        re-arm, cancel, or leave it armed for the next scheduled attempt.
        """
        self.fires += 1
        self.last_fired_at = now
        self.last_error = error
        if (
            self.max_fires is not None
            and self.fires >= self.max_fires
            and not self.is_terminal
        ):
            self.transition("completed")
        return self

    def is_expired(self, now: datetime) -> bool:
        """Whether ``expires_at`` has passed for a still-resting trigger."""
        return (
            self.expires_at is not None
            and not self.is_terminal
            and now >= self.expires_at
        )

    def next_fire_at(self, now: datetime) -> datetime | None:
        """Next wall-clock due time, or ``None`` for event-driven kinds.

        Default (base implementation, and every non-time kind): never due by
        polling the clock — ``ci.workflow``/``forge.pr``/``webhook`` fire via
        ``matches()``/``verify()`` on an inbound event instead.
        """
        return None

    def is_due(self, now: datetime) -> bool:
        """Whether a scheduler should fire this trigger right now.

        Only ``armed`` triggers are ever due — a ``paused`` or terminal
        trigger never polls positive, regardless of what ``next_fire_at``
        would otherwise say.
        """
        if self.status != "armed":
            return False
        next_at = self.next_fire_at(now)
        return next_at is not None and next_at <= now

    # -- parsing ----------------------------------------------------------

    # Lazily built on first `parse()` call and cached on the base class — every
    # kind resolves through the same adapter instance. Left unparameterized
    # (rather than `TypeAdapter[TriggerUnion]`) so this ClassVar declaration
    # doesn't force `TriggerUnion` to exist yet at `TriggerSpec`'s own class-body
    # evaluation time; `TriggerUnion` is defined once every kind below it is.
    _adapter: ClassVar[TypeAdapter | None] = None

    @classmethod
    def parse(cls, data: Mapping[str, object]) -> TriggerSpec:
        """Parse a raw dict (JSON-decoded or Mongo document) into its concrete kind.

        The ONE parse seam (see the module docstring): the discriminated-union
        ``TypeAdapter`` is expensive to build, so it's constructed lazily on
        first use rather than at import time.
        """
        if TriggerSpec._adapter is None:
            TriggerSpec._adapter = TypeAdapter(TriggerUnion)
        return TriggerSpec._adapter.validate_python(data)


# ---------------------------------------------------------------------------
# Concrete kinds
# ---------------------------------------------------------------------------


class TimeAtTrigger(TriggerSpec):
    """Fire once at a specific wall-clock instant."""

    kind: Literal["time.at"] = "time.at"
    at: datetime

    @field_validator("at")
    @classmethod
    def _validate_at(cls, value: datetime) -> datetime:
        return cls._require_aware(value)

    @model_validator(mode="after")
    def _force_single_fire(self) -> TimeAtTrigger:
        """A one-shot alarm can only ever fire once — force ``max_fires = 1``."""
        self.max_fires = 1
        return self

    def next_fire_at(self, now: datetime) -> datetime | None:
        """Return ``at`` until the first (and only) fire has happened."""
        if self.fires > 0:
            return None
        return self.at


class CronTrigger(TriggerSpec):
    """Fire repeatedly on a cron schedule."""

    kind: Literal["time.cron"] = "time.cron"
    cron: str

    @field_validator("cron")
    @classmethod
    def _validate_cron(cls, value: str) -> str:
        if not croniter.is_valid(value):
            raise ValueError(f"invalid cron expression: {value!r}")
        return value

    def next_fire_at(self, now: datetime) -> datetime | None:
        """Next occurrence strictly after the last fire (or creation).

        Anchoring on ``last_fired_at``/``created_at`` rather than ``now``
        means a boundary-exact poll never gets skipped past — ``is_due``
        still gates on ``next_at <= now`` for the actual fire decision.
        """
        base = self.last_fired_at or self.created_at
        return croniter(self.cron, base).get_next(datetime)


class CiWorkflowTrigger(TriggerSpec):
    """Fire when a CI run on ``repo`` completes and matches the filter."""

    kind: Literal["ci.workflow"] = "ci.workflow"
    repo: str
    run_id: int | None = None
    workflow: str | None = None
    ref: str | None = None
    conclusion_filter: list[str] | None = None

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        return cls._validate_owner_repo(value)

    @model_validator(mode="after")
    def _exactly_one_selector(self) -> CiWorkflowTrigger:
        if (self.run_id is None) == (self.workflow is None):
            raise ValueError("ci.workflow trigger requires exactly one of run_id or workflow")
        return self

    def matches(self, payload: Mapping[str, object]) -> bool:
        """Whether a normalized CI-run payload satisfies this trigger.

        Expected payload shape: ``{"repo", "run_id", "workflow", "ref",
        "status", "conclusion"}`` (normalization from the forge-specific
        webhook shape happens upstream — this method only compares already
        normalized fields). Only a ``status == "completed"`` run can match.
        """
        if payload.get("repo") != self.repo:
            return False
        if self.run_id is not None and payload.get("run_id") != self.run_id:
            return False
        if self.workflow is not None and payload.get("workflow") != self.workflow:
            return False
        if self.ref is not None and payload.get("ref") != self.ref:
            return False
        if payload.get("status") != "completed":
            return False
        if self.conclusion_filter and payload.get("conclusion") not in self.conclusion_filter:
            return False
        return True


class ForgePrTrigger(TriggerSpec):
    """Fire on selected lifecycle events of a forge pull request."""

    kind: Literal["forge.pr"] = "forge.pr"
    repo: str
    number: int
    events: list[Literal["merged", "review", "comment", "ci_status"]]

    @field_validator("repo")
    @classmethod
    def _validate_repo(cls, value: str) -> str:
        return cls._validate_owner_repo(value)

    @field_validator("events")
    @classmethod
    def _non_empty_events(
        cls, value: list[Literal["merged", "review", "comment", "ci_status"]]
    ) -> list[Literal["merged", "review", "comment", "ci_status"]]:
        if not value:
            raise ValueError("events must be non-empty")
        return value

    def matches(self, payload: Mapping[str, object]) -> bool:
        """Whether a normalized PR-event payload satisfies this trigger.

        Expected payload shape: ``{"repo", "number", "event"}`` where
        ``event`` is one of the same literals as :attr:`events`.
        """
        if payload.get("repo") != self.repo:
            return False
        if payload.get("number") != self.number:
            return False
        return payload.get("event") in self.events


class WebhookTrigger(TriggerSpec):
    """Fire when an inbound webhook presents the right secret (+ optional HMAC)."""

    kind: Literal["webhook"] = "webhook"
    secret: str = Field(default_factory=lambda: secrets.token_hex(16))
    hmac_header: str | None = None

    def verify(self, secret_from_url: str, headers: Mapping[str, str], body: bytes) -> bool:
        """Constant-time verify the URL secret, plus an HMAC-SHA256 over ``body``.

        The HMAC check only runs when ``hmac_header`` is configured; the
        header value may carry an ``algo=`` prefix (e.g. ``sha256=...``),
        which is stripped before comparison.
        """
        if not hmac.compare_digest(secret_from_url, self.secret):
            return False
        if self.hmac_header is None:
            return True
        provided = headers.get(self.hmac_header, "")
        if "=" in provided:
            _, _, provided = provided.rpartition("=")
        expected = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.lower(), expected.lower())


TriggerUnion = Annotated[
    TimeAtTrigger | CronTrigger | CiWorkflowTrigger | ForgePrTrigger | WebhookTrigger,
    Field(discriminator="kind"),
]

# Thin module-level delegation to the class-owned implementation — kept for the
# frozen public API (mirrors the `create_session_store`-style ecosystem
# convention of factory functions living at module level).
parse_trigger = TriggerSpec.parse


__all__ = [
    "TriggerStatus",
    "TriggerProvenance",
    "TriggerAuthority",
    "TriggerSpec",
    "TimeAtTrigger",
    "CronTrigger",
    "CiWorkflowTrigger",
    "ForgePrTrigger",
    "WebhookTrigger",
    "TriggerUnion",
    "parse_trigger",
]
