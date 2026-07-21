"""Durable purpose-binding for a session — the spec every re-engage path reads.

What a session is FOR (which surface created it, against which project/cwd, on
what model ladder, under which tool ceiling and capabilities) used to live only
as untyped ``context``-event keys that each re-engage site re-read by hand. One
path — ``POST /sessions/<id>/query`` — never read them at all: it re-derived the
model from config and then PERSISTED that default, so a console follow-up into a
purpose-bound session arrived on a different model, with a different tool set, no
playbook, and a cwd pointing at an empty per-session temp dir.

:class:`SessionSpec` formalizes the convention the wiki-QA drive already
established — persist the scope as first-class session state rather than as bare
``start_async`` kwargs — instead of inventing a second one beside it. The spec is
written INTO an ordinary ``context`` event that carries BOTH the loose legacy
keys every existing reader still consumes (``model``/``mcp_tools``/
``strict_tool_scope``/``skill_instructions``/…) AND a typed ``session_spec``
mirror. Writing the full payload is load-bearing, not tidiness: a context reader
takes the most-recent event's payload VERBATIM, so an event carrying only the
typed blob would blank every legacy field for ``/message``, ``/recover`` and the
trigger wake.

The model owns the one decision that matters — :meth:`SessionSpec.merge_request_overrides`,
which field a request may legitimately override. A user changing the model is
legitimate; a surface silently swapping the tool set out from under a
purpose-bound session is not. The ``editable`` projection a front end trusts is
derived from that SAME predicate, so the two can never drift.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any, ClassVar

from mewbo_core.session_provenance import SessionOrigin
from pydantic import BaseModel, ConfigDict, Field, field_validator

# The context-payload key the typed mirror rides under. A session whose newest
# context event carries this key is spec-bound; anything else is a legacy session
# whose binding is reconstructed from the loose keys (``SessionSpec.from_context``).
SPEC_CONTEXT_KEY = "session_spec"


class SessionSpec(BaseModel):
    """The durable configuration a session was created to run under.

    Written once at session creation, derived FROM the origin surface (an indexer
    session assumes its job's model/tools/cwd, a search session its tier preset, a
    console chat the composer's choices at creation), and read by every later turn
    through ANY surface rather than re-derived from whoever happens to be calling.
    """

    model_config = ConfigDict(extra="forbid")

    origin: SessionOrigin = Field(
        SessionOrigin.USER,
        description="Provenance of the creating surface; the purpose this session is bound to.",
    )
    surface: str | None = Field(
        None, description="Originating client surface (the X-Mewbo-Surface value at creation)."
    )
    project: str | None = Field(None, description="Configured or managed project name.")
    slug: str | None = Field(None, description="Product-scoped identifier (a wiki project slug).")
    cwd: str | None = Field(None, description="Working directory the session is anchored to.")
    model: str | None = Field(None, description="Model the session was created to run on.")
    fallback_models: tuple[str, ...] | None = Field(
        None,
        description="Opted-in fallback ladder; None defers to the configured fallback policy.",
    )
    allowed_tools: tuple[str, ...] | None = Field(
        None,
        description=(
            "MCP tool ceiling. THREE-STATE and every test on it must be `is None`: "
            "None unrestricted, () grants no MCP tool, non-empty grants exactly those. "
            "Truthiness collapses () into None, turning 'no tools' into 'every tool'."
        ),
    )
    strict_tool_scope: bool = Field(
        False, description="Whether the allowlist is authoritative over built-ins too."
    )
    skill_instructions: str | None = Field(
        None, description="Playbook text the session was started with (a wiki/QA AgentDef body)."
    )
    capabilities: tuple[str, ...] | None = Field(
        None,
        description=(
            "Capabilities the session's PURPOSE requires. Deliberately not overridable: an "
            "unattended fire re-derives from this, never from whatever an interactive turn "
            "last advertised."
        ),
    )
    session_step_budget: int | None = Field(
        None, description="Per-session step budget; None defers to the configured default."
    )
    mode: str | None = Field(None, description="Orchestration mode, 'plan' or 'act'.")

    # Fields a request may ALWAYS override. Choosing a model (and the ladder that
    # backs it, and whether this turn plans or acts) is the user's call on any
    # session — the deliberate act the composer exists to express.
    ALWAYS_OVERRIDABLE: ClassVar[frozenset[str]] = frozenset({"model", "fallback_models", "mode"})
    # Fields a request may override only on a session that is NOT purpose-bound. On
    # a bound session these ARE the binding: letting a caller replace them is
    # exactly the drift that sent a console follow-up into an indexer session with
    # 108 unrelated tools and no playbook.
    OVERRIDABLE_WHEN_UNBOUND: ClassVar[frozenset[str]] = frozenset(
        {
            "project",
            "slug",
            "cwd",
            "allowed_tools",
            "strict_tool_scope",
            "skill_instructions",
            "session_step_budget",
        }
    )
    # Never overridable through a request on any session. ``origin``/``surface`` are
    # creation facts; ``capabilities`` is the purpose contract the unattended-fire
    # re-derivation turns on.
    NEVER_OVERRIDABLE: ClassVar[frozenset[str]] = frozenset({"origin", "surface", "capabilities"})
    # Capabilities that only mean anything with a human attached to the run: bound
    # on an interactive turn, stripped from every unattended fire.
    INTERACTIVE_ONLY_CAPABILITIES: ClassVar[frozenset[str]] = frozenset({"ask_user"})

    # field name → the loose ``context``-event key it has always been persisted
    # under. ONE vocabulary: the overrides parser, the persisted payload and the
    # "which keys does the spec own" guard all read it, so a renamed key cannot
    # move in one of the three and not the others.
    CONTEXT_WIRE_NAMES: ClassVar[dict[str, str]] = {
        "project": "project",
        "slug": "slug",
        "cwd": "cwd",
        "model": "model",
        "fallback_models": "fallback_models",
        "allowed_tools": "mcp_tools",
        "strict_tool_scope": "strict_tool_scope",
        "skill_instructions": "skill_instructions",
        "session_step_budget": "session_step_budget",
        "mode": "mode",
        "capabilities": "client_capabilities",
    }
    # Context keys the spec is AUTHORITATIVE for. A caller that merges leftover
    # request keys onto the persisted payload must skip these: re-adding a raw
    # request value for a field the merge just REFUSED would hand the override
    # back through the side door, which is the whole defect one layer up.
    SPEC_OWNED_CONTEXT_KEYS: ClassVar[frozenset[str]] = frozenset(
        {*CONTEXT_WIRE_NAMES.values(), SPEC_CONTEXT_KEY}
    )

    # ── shared field normalizers (the owner of the field semantics) ──────────

    @staticmethod
    def normalize_ids(value: object) -> tuple[str, ...] | None:
        """Normalize a wire list of ids to a tuple, PRESERVING an empty one.

        The empty case survives as ``()`` rather than collapsing to ``None`` because
        for ``allowed_tools`` those mean opposite things — see the three-state law on
        that field. Fields where empty genuinely means absent (the fallback ladder)
        apply that themselves.
        """
        if value is None or isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
            return None
        return tuple(str(item).strip() for item in value if str(item).strip())

    @staticmethod
    def normalize_text(value: object) -> str | None:
        """An empty or whitespace-only string is an ABSENT binding, not a bound empty one."""
        if not isinstance(value, str):
            return None
        return value.strip() or None

    @staticmethod
    def normalize_budget(value: object) -> int | None:
        """A non-positive or non-integer budget is no override; the config default applies."""
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value if value > 0 else None

    @staticmethod
    def normalize_mode(value: object) -> str | None:
        """Only the two orchestration modes survive; anything else is no declaration."""
        if not isinstance(value, str):
            return None
        lowered = value.strip().lower()
        return lowered if lowered in {"plan", "act"} else None

    # ── validation at definition ────────────────────────────────────────────

    @field_validator("surface", "project", "slug", "cwd", "model", "skill_instructions",
                     mode="before")
    @classmethod
    def _clean_text(cls, value: object) -> str | None:
        return cls.normalize_text(value)

    @field_validator("allowed_tools", "capabilities", mode="before")
    @classmethod
    def _clean_ids(cls, value: object) -> tuple[str, ...] | None:
        return cls.normalize_ids(value)

    @field_validator("fallback_models", mode="before")
    @classmethod
    def _clean_ladder(cls, value: object) -> tuple[str, ...] | None:
        """An empty ladder is no opt-in, i.e. the same as absent (defer to config).

        Unlike ``allowed_tools`` an empty tuple here is NOT a meaningful ceiling — an
        explicit empty ladder would DISABLE the auto-heal chain, which no caller has
        ever meant to request.
        """
        return cls.normalize_ids(value) or None

    @field_validator("mode", mode="before")
    @classmethod
    def _clean_mode(cls, value: object) -> str | None:
        return cls.normalize_mode(value)

    @field_validator("session_step_budget", mode="before")
    @classmethod
    def _clean_budget(cls, value: object) -> int | None:
        return cls.normalize_budget(value)

    # ── construction ────────────────────────────────────────────────────────

    @classmethod
    def from_context(
        cls,
        context_payload: dict[str, object] | None,
        *,
        origin: SessionOrigin | None = None,
        surface: str | None = None,
    ) -> SessionSpec:
        """Reconstruct a binding from a legacy ``context`` payload's loose keys.

        The fallback for every session created before the typed mirror existed, and
        for the surfaces that still persist their scope as loose keys. Reads the SAME
        keys the individual ``_extract_*`` readers do, so a legacy session's
        reconstructed binding matches what those readers already resolved.
        """
        payload = context_payload or {}
        blob = payload.get(SPEC_CONTEXT_KEY)
        if isinstance(blob, dict):
            return cls.from_blob(blob)
        # ``model_validate`` (an ``obj: Any`` boundary), not ``cls(...)`` (a
        # per-field-typed constructor): every value below is read out of an
        # untyped ``payload`` as ``object``, and the model's own "before"
        # validators are what narrow it — the same narrowing a direct
        # keyword construction would trigger, just without asking the
        # per-field constructor signature to trust an untyped dict read.
        return cls.model_validate(
            {
                "origin": origin or SessionOrigin.classify([], payload),
                "surface": surface or payload.get("source_platform"),
                "project": payload.get("project"),
                "slug": payload.get("slug"),
                "cwd": payload.get("cwd"),
                "model": payload.get("model"),
                "fallback_models": payload.get("fallback_models"),
                "allowed_tools": payload.get("mcp_tools"),
                "strict_tool_scope": bool(payload.get("strict_tool_scope", False)),
                "skill_instructions": payload.get("skill_instructions"),
                "capabilities": payload.get("client_capabilities"),
                "session_step_budget": payload.get("session_step_budget"),
                "mode": payload.get("mode"),
            }
        )

    @classmethod
    def from_blob(cls, blob: dict[str, object]) -> SessionSpec:
        """Parse a persisted typed mirror, tolerating a shape written by an older build.

        A stored spec is READ on the request path of an already-created session, so a
        key this build no longer knows must never brick the session it describes —
        unknown keys are dropped and the rest is honoured. Same reasoning as the
        system-instructions doc validating its template on WRITE rather than at
        definition: a stored artifact whose only reader is the surface that would let
        someone FIX it has to stay loadable.
        """
        known = {key: value for key, value in blob.items() if key in cls.model_fields}
        return cls.model_validate(known)

    # ── binding semantics ───────────────────────────────────────────────────

    @property
    def purpose_bound(self) -> bool:
        """Whether this session was created FOR something narrower than open chat.

        Two independent signals, either sufficient: a non-``user`` origin (a product
        surface created it), or an authoritative tool scope. A plain console chat is
        neither, so its composer keeps supplying the tool set per turn exactly as it
        always has — locking that would strand every ordinary session on whichever
        MCP servers happened to be reachable at creation.
        """
        return self.origin is not SessionOrigin.USER or self.strict_tool_scope

    def field_editable(self, field: str) -> bool:
        """Whether a request may override *field* on this session.

        The ONE predicate behind both :meth:`merge_request_overrides` (enforcement)
        and :meth:`editable_fields` (the projection a front end trusts) — a second
        hand-written table would drift the first time a field changed tier.
        """
        if field in self.ALWAYS_OVERRIDABLE:
            return True
        if field in self.OVERRIDABLE_WHEN_UNBOUND:
            return not self.purpose_bound
        return False

    def editable_fields(self) -> dict[str, bool]:
        """Server-declared per-field modifiability, so a client disables rather than guesses.

        Fail-closed like the wiki project-settings map it mirrors: a field absent from
        this map is not editable, never editable-by-default.
        """
        return {
            field: self.field_editable(field)
            for field in type(self).model_fields
            if field not in self.NEVER_OVERRIDABLE
        }

    def merge_request_overrides(
        self, overrides: SessionSpecOverrides
    ) -> tuple[SessionSpec, tuple[str, ...]]:
        """Apply the overrides this session sanctions; return the run spec + what was refused.

        Only fields the request EXPLICITLY set are considered — absence inherits. That
        asymmetry is the whole fix: the corrupting behaviour was a request that merely
        OMITTED a model / tool set / cwd being read as a request to RESET them to
        defaults, which then got persisted over the real binding.

        Refused names are returned rather than swallowed so the caller can say so out
        loud; a silently-dropped override is indistinguishable from an applied one at
        the point where it matters.

        Re-validates rather than ``model_copy(update=...)``: ``model_copy`` skips
        every field validator, so an externally-sourced override (a blank ``model``,
        a negative ``session_step_budget``, an empty ``fallback_models``) would land
        on the merged spec un-normalized instead of collapsing to the "no override"
        value its own definition-time normalizer guarantees everywhere else.
        """
        applied: dict[str, object] = {}
        refused: list[str] = []
        for field, value in overrides.declared().items():
            if self.field_editable(field):
                applied[field] = value
            else:
                refused.append(field)
        merged = (
            type(self).model_validate({**self.model_dump(), **applied}) if applied else self
        )
        return merged, tuple(refused)

    def run_capabilities(self, requested: Sequence[str] | None) -> tuple[str, ...] | None:
        """Capabilities for ONE interactive turn: what the caller advertised, else the purpose.

        An interactive client may legitimately advertise more than the purpose needs (a
        console declaring ``ask_user`` because a human is watching). That widening is
        turn-scoped by construction — it never reaches the spec, because ``capabilities``
        is not overridable — so it cannot leak into a later unattended fire, which reads
        :meth:`unattended_capabilities` instead.
        """
        advertised = self.normalize_ids(requested)
        if advertised:
            return advertised
        return self.capabilities

    def unattended_capabilities(self) -> tuple[str, ...] | None:
        """Capabilities for a fire with nobody watching — re-derived from the purpose.

        ``allowed_tools`` is already recomputed per fire; capabilities were not, so one
        interactive turn that advertised ``ask_user`` left every subsequent scheduled
        fire able to bind a tool that BLOCKS until a human answers — with no human.
        Re-deriving from the spec closes that, and the interactive-only capabilities are
        dropped explicitly so a purpose that legitimately carries one still cannot arm
        it unattended.
        """
        if self.capabilities is None:
            return None
        return tuple(
            cap for cap in self.capabilities if cap not in self.INTERACTIVE_ONLY_CAPABILITIES
        )

    # ── projections ─────────────────────────────────────────────────────────

    def to_context_payload(self, *, capabilities: Sequence[str] | None = None) -> dict[str, object]:
        """The ``context``-event payload that persists this binding.

        Carries the loose legacy keys AND the typed mirror, because a context reader
        takes the newest event's payload VERBATIM — a blob-only event would blank
        ``model``/``mcp_tools``/``strict_tool_scope`` for ``/message``, ``/recover`` and
        the trigger wake. *capabilities* overrides only the loose ``client_capabilities``
        key (this turn's advertisement); the mirror always records the durable purpose set.
        """
        turn_capabilities = self.normalize_ids(capabilities)
        if turn_capabilities is None:
            turn_capabilities = self.capabilities
        payload: dict[str, object] = {SPEC_CONTEXT_KEY: self.model_dump(mode="json")}
        loose: dict[str, object | None] = {
            "project": self.project,
            "slug": self.slug,
            "cwd": self.cwd,
            "model": self.model,
            "skill_instructions": self.skill_instructions,
            "mode": self.mode,
            "session_step_budget": self.session_step_budget,
            "fallback_models": list(self.fallback_models) if self.fallback_models else None,
            "client_capabilities": list(turn_capabilities) if turn_capabilities else None,
        }
        payload.update({key: value for key, value in loose.items() if value is not None})
        # Three-state: an empty allowlist is a real ceiling and must persist as [], so
        # this key is written whenever it is not None — never filtered by falsiness.
        if self.allowed_tools is not None:
            payload["mcp_tools"] = list(self.allowed_tools)
        if self.strict_tool_scope:
            payload["strict_tool_scope"] = True
        return payload

    def projection(self) -> dict[str, object]:
        """The wire shape of the binding itself (snake_case, matching the session surface).

        ``skill_instructions`` projects as a PRESENCE flag: the value is a whole AgentDef
        playbook body, which no client renders and which would dominate the response. A
        binding panel needs to know a playbook is bound, not what it says.
        """
        return {
            "origin": self.origin.value,
            "surface": self.surface,
            "purpose_bound": self.purpose_bound,
            "project": self.project,
            "slug": self.slug,
            "cwd": self.cwd,
            "model": self.model,
            "fallback_models": list(self.fallback_models) if self.fallback_models else None,
            "allowed_tools": list(self.allowed_tools) if self.allowed_tools is not None else None,
            "strict_tool_scope": self.strict_tool_scope,
            "capabilities": list(self.capabilities) if self.capabilities else None,
            "skill_instructions_present": self.skill_instructions is not None,
            "session_step_budget": self.session_step_budget,
            "mode": self.mode,
        }


class SessionSpecOverrides(BaseModel):
    """The subset of a binding a single request explicitly declared.

    Omit-vs-null semantics via ``model_fields_set``, mirroring the wiki settings patch
    model: an ABSENT field inherits the session's binding, an explicitly-sent one asks
    to override it. Reading absence as "reset to the default" is the exact defect this
    model exists to make unrepresentable.
    """

    model_config = ConfigDict(extra="forbid")

    project: str | None = None
    slug: str | None = None
    cwd: str | None = None
    model: str | None = None
    fallback_models: tuple[str, ...] | None = None
    allowed_tools: tuple[str, ...] | None = None
    strict_tool_scope: bool | None = None
    skill_instructions: str | None = None
    session_step_budget: int | None = None
    mode: str | None = None

    @field_validator("allowed_tools", "fallback_models", mode="before")
    @classmethod
    def _clean_ids(cls, value: object) -> tuple[str, ...] | None:
        """Normalize declared id sequences through the field's OWNER, never a second copy."""
        return SessionSpec.normalize_ids(value)

    def declared(self) -> dict[str, object]:
        """Only the fields this request actually sent (never the model's own defaults)."""
        return {field: getattr(self, field) for field in self.model_fields_set}

    @classmethod
    def from_request_context(
        cls,
        context_payload: dict[str, object] | None,
        *,
        cwd: str | None = None,
        mode: str | None = None,
        skill_instructions: str | None = None,
    ) -> SessionSpecOverrides:
        """Build the declared set from a request's context payload.

        PRESENCE in the payload is what marks a field as declared, so this walks the
        wire keys rather than constructing the model with defaults — passing
        ``model=None`` for an absent key would DECLARE a null override and clear the
        binding, the same class of bug one layer up. *cwd* / *mode* /
        *skill_instructions* arrive separately because each is resolved by its own
        policy (external-cwd validation, the top-level ``mode`` field, skill
        activation) before it can count as a declaration.
        """
        payload = context_payload or {}
        declared: dict[str, object] = {}
        for field in cls.model_fields:
            key = SessionSpec.CONTEXT_WIRE_NAMES.get(field)
            # ``cwd``/``mode``/``skill_instructions`` are excluded from the payload
            # walk on purpose: each arrives as an explicit argument only AFTER its
            # own policy ran. Reading a raw ``context.cwd`` here would let a caller
            # declare a working directory that never passed the external-cwd gate.
            if key is None or field in {"cwd", "mode", "skill_instructions"}:
                continue
            if key in payload:
                declared[field] = payload[key]
        if cwd is not None:
            declared["cwd"] = cwd
        if mode is not None:
            declared["mode"] = mode
        if skill_instructions is not None:
            declared["skill_instructions"] = skill_instructions
        return cls.model_validate(declared)


class SessionSpecStore:
    """Reads and writes a session's binding over its transcript.

    The transcript IS the store — there is no parallel spec collection, because the
    binding has to travel with the events every other surface already reads (Mongo
    forensics, the export payload, a fork). Collaborators arrive as injected callables
    rather than a whole runtime, so a test drives this against an in-memory list with
    no session store at all.
    """

    def __init__(
        self,
        *,
        load_transcript: Callable[[str], list[dict[str, Any]]],
        append_context_event: Callable[[str, dict[str, object]], None],
    ) -> None:
        """Bind the two transcript operations this store needs."""
        self._load_transcript = load_transcript
        self._append_context_event = append_context_event

    def load(self, session_id: str) -> SessionSpec:
        """The session's binding: the newest typed mirror, else the legacy reconstruction.

        Scans back for a context event carrying the mirror and falls back to the newest
        context payload's loose keys. That fallback deliberately reads ONE payload rather
        than merging every context event: merging would resurrect a field the user
        cleared (a removed project sticking forever), the same reason the generic context
        reader never merges either.
        """
        newest_context: dict[str, object] | None = None
        for event in reversed(self._transcript(session_id)):
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            if isinstance(payload.get(SPEC_CONTEXT_KEY), dict):
                try:
                    return SessionSpec.from_blob(payload[SPEC_CONTEXT_KEY])
                except Exception:  # noqa: BLE001 - a stored spec must never brick its session
                    return SessionSpec.from_context(payload)
            if newest_context is None:
                newest_context = payload
        return SessionSpec.from_context(newest_context)

    def save(
        self, session_id: str, spec: SessionSpec, *, capabilities: Sequence[str] | None = None
    ) -> None:
        """Persist *spec* as a full context event (typed mirror + legacy loose keys)."""
        self._append_context_event(session_id, spec.to_context_payload(capabilities=capabilities))

    def _transcript(self, session_id: str) -> list[dict[str, Any]]:
        """Load the transcript, degrading to empty rather than failing a request path."""
        try:
            return self._load_transcript(session_id)
        except Exception:  # noqa: BLE001 - an unreadable transcript means "no binding yet"
            return []
