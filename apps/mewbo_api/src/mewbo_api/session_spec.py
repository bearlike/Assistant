"""Durable purpose-binding for a session — the spec every re-engage path reads.

What a session is FOR — which surface created it, against which project/cwd, on
what model ladder, under which tool ceiling and capabilities — is first-class
session state, not a set of untyped ``context``-event keys each re-engage site
re-reads by hand. A site that re-derives one of them from config instead (and
then PERSISTS that default) lands a console follow-up on a different model, with
a different tool set, no playbook, and a cwd pointing at an empty per-session
temp dir.

:class:`SessionSpec` is that state, persisted rather than passed as bare
``start_async`` kwargs. The spec is
written INTO an ordinary ``context`` event that carries BOTH the loose
keys every existing reader consumes (``model``/``mcp_tools``/
``strict_tool_scope``/``skill_instructions``/…) AND a typed ``session_spec``
mirror. Writing the full payload is load-bearing, not tidiness: a context reader
takes the most-recent event's payload VERBATIM, so an event carrying only the
typed blob would blank every loose field for ``/message``, ``/recover`` and the
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

from mewbo_core.capabilities import ASK_USER_CAPABILITY, SCG_CAPABILITY, WIKI_CAPABILITY
from mewbo_core.session.session_provenance import SessionOrigin
from mewbo_core.session.session_store import SessionStoreBase
from pydantic import BaseModel, ConfigDict, Field, field_validator

# The context-payload key the typed mirror rides under. A session whose newest
# context event carries this key is spec-bound; without it the binding is
# reconstructed from the loose keys (``SessionSpec.from_context``).
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
    denied_tools: tuple[str, ...] | None = Field(
        None,
        description=(
            "Tool ids withheld regardless of any other gate — including a built-in "
            "session tool's unconditional or capability auto-surface. Deliberately "
            "NOT three-state like `allowed_tools`: deny is purely subtractive, so "
            "None and () are the same 'nothing denied' set."
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
    # session — the deliberate act the composer exists to express. ``denied_tools``
    # joins them for a different reason: unlike ``allowed_tools`` it is purely
    # SUBTRACTIVE, so a caller can only ever narrow a purpose-bound session's tool
    # surface further, never widen it back toward the binding the OVERRIDABLE_
    # WHEN_UNBOUND tier exists to protect.
    ALWAYS_OVERRIDABLE: ClassVar[frozenset[str]] = frozenset(
        {"model", "fallback_models", "mode", "denied_tools"}
    )
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
    INTERACTIVE_ONLY_CAPABILITIES: ClassVar[frozenset[str]] = frozenset(
        {ASK_USER_CAPABILITY}
    )
    # Capabilities that belong to what a session IS FOR rather than to whoever is
    # driving this request, so they must survive a re-engage from any surface. A
    # viewer re-engaging a wiki session still reasons about ``wiki``, and the
    # client advertising `stlite,apps,ask_user,generative_ui` on every request
    # would otherwise bury it — the advertisement is what a CLIENT can render, not
    # what the session was created to do.
    #
    # Deliberately a CLOSED allowlist naming only the substrate suites, not
    # "everything that is not a rendering capability". Widening a session's reach
    # is the failure that matters here, so an unrecognised capability must fall on
    # the side that grants nothing extra; a new substrate capability is expected to
    # add itself here, exactly as it already must add itself to a plugin manifest.
    # ``apps`` is NOT in this set even though it gates a plugin suite: the console
    # advertises it on every request regardless of what the session is for (the
    # same promiscuity that once made ``SessionOrigin`` file every ordinary chat
    # under ``apps``), so treating it as session-owned would union it onto sessions
    # that merely happened to be created from a browser.
    SESSION_OWNED_CAPABILITIES: ClassVar[frozenset[str]] = frozenset(
        {WIKI_CAPABILITY, SCG_CAPABILITY}
    )

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
        "denied_tools": "denied_tools",
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

    @field_validator("fallback_models", "denied_tools", mode="before")
    @classmethod
    def _clean_subtractive_ids(cls, value: object) -> tuple[str, ...] | None:
        """An empty set collapses to absent — for two DIFFERENT reasons.

        Unlike ``allowed_tools`` an empty ``fallback_models`` is NOT a meaningful
        ceiling — an explicit empty ladder would DISABLE the auto-heal chain, which
        no caller has ever meant to request. ``denied_tools`` collapses for the
        opposite-flavoured reason stated on the field itself: deny is purely
        subtractive, so an empty denylist and no denylist are the same set — there
        is no third "deny nothing, on purpose" state to preserve.
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
        """Reconstruct a binding from a ``context`` payload's loose keys.

        The fallback for a session carrying no typed mirror, and for the surfaces
        that persist their scope as loose keys. Reads the SAME keys the individual
        ``_extract_*`` readers do, so the reconstructed binding matches what those
        readers already resolved.
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
                "denied_tools": payload.get("denied_tools"),
                "strict_tool_scope": bool(payload.get("strict_tool_scope", False)),
                "skill_instructions": payload.get("skill_instructions"),
                "capabilities": payload.get("client_capabilities"),
                "session_step_budget": payload.get("session_step_budget"),
                "mode": payload.get("mode"),
            }
        )

    @classmethod
    def from_blob(
        cls, blob: dict[str, object], *, origin: SessionOrigin | None = None
    ) -> SessionSpec:
        """Parse a persisted typed mirror, tolerating a shape written by an older build.

        A stored spec is READ on the request path of an already-created session, so a
        key this build does not know must never brick the session it describes —
        unknown keys are dropped and the rest is honoured. Same reasoning as the
        system-instructions doc validating its template on WRITE rather than at
        definition: a stored artifact whose only reader is the surface that would let
        someone FIX it has to stay loadable.

        *origin* OVERRIDES the blob's own stored value when given, rather than this
        method re-deriving it (a pure model classmethod takes no store/tag access —
        the clock/webhook-payload rule applies here too). ``SessionOrigin`` is
        documented elsewhere as derived at read time and never stored, but a blob's
        ``origin`` field is exactly a value stamped once at write time by whichever
        classifier ran then: leaving it untouched freezes a session under a
        classification a later classifier fix would otherwise correct for every
        other session. Omit *origin* only for a caller with no tag/context source to
        re-derive from (a bare unit test constructing a blob directly).
        """
        known = {key: value for key, value in blob.items() if key in cls.model_fields}
        if origin is not None:
            known["origin"] = origin
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
        """Capabilities for ONE turn: the purpose UNIONED with this caller's advertisement.

        An interactive client may legitimately advertise more than the purpose needs (a
        console declaring ``ask_user`` because a human is watching). That widening is
        turn-scoped by construction — it never reaches the spec, because ``capabilities``
        is not overridable — so it cannot leak into a later unattended fire, which reads
        :meth:`unattended_capabilities` instead.

        **It is a union, not a replacement, and that is a capability-gate fix rather
        than a preference.** ``client_capabilities`` carries two different kinds of
        thing (see :attr:`SESSION_OWNED_CAPABILITIES`). Returning the advertisement
        alone let a client that stamps a fixed rendering set on every request bury the
        purpose: re-engaging a wiki session bound the console's four rendering
        capabilities and NOT ``wiki``, so ``SessionToolRegistry.build_for`` selected no
        ``wiki_*`` factory and the agent fell back to browsing the repository through
        generic tools. Only the SESSION-OWNED half is carried across; a rendering
        capability still comes solely from whoever is driving this request, so a
        surface that cannot render one never inherits it from the spec.
        """
        advertised = self.normalize_ids(requested)
        if not advertised:
            # Nothing advertised at all (an unattended fire, or a client that sends no
            # header): the purpose is the only thing there is to bind.
            return self.capabilities
        owned = tuple(
            cap for cap in (self.capabilities or ()) if cap in self.SESSION_OWNED_CAPABILITIES
        )
        # ``dict.fromkeys`` de-dupes while preserving order, so a client that already
        # advertised the session-owned capability gets a byte-identical set back.
        return tuple(dict.fromkeys((*owned, *advertised)))

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

        Carries the loose keys AND the typed mirror, because a context reader
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
        # NOT three-state — an empty denylist is the same as no denylist, so this
        # is safe to filter by falsiness like every other loose key above.
        if self.denied_tools:
            payload["denied_tools"] = list(self.denied_tools)
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
            "denied_tools": list(self.denied_tools) if self.denied_tools else None,
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
    denied_tools: tuple[str, ...] | None = None
    strict_tool_scope: bool | None = None
    skill_instructions: str | None = None
    session_step_budget: int | None = None
    mode: str | None = None

    @field_validator("allowed_tools", "denied_tools", "fallback_models", mode="before")
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
        load_tags: Callable[[str], Sequence[str]] | None = None,
        latest_event_of_type: (
            Callable[[str, str, str | None], dict[str, Any] | None] | None
        ) = None,
    ) -> None:
        """Bind the transcript operations this store needs.

        *load_tags* is OPTIONAL and defaults to "this session has no tags", so a
        caller driving the store off a bare event list (a test) needs no tag
        source at all. A production caller SHOULD supply it — without it a
        session whose binding has to be reconstructed is classified from its
        context alone, which is exactly the blind spot :meth:`origin_for` exists
        to close.

        *latest_event_of_type* is the store's type-bounded read
        (``SessionStoreBase.latest_event_of_type``), and it is what makes
        :meth:`load` cost one document instead of a whole transcript. It is
        OPTIONAL for the same reason *load_tags* is: this class is documented as
        drivable off a bare event list with no session store at all, and a
        caller in that mode has no such primitive to hand over. Absent, the
        binding is read by scanning the transcript the caller DID supply —
        the identical answer at the base driver's honest ``O(one session)``,
        which is exactly the split ``SessionStoreBase`` publishes between its
        own template and the Mongo override. Refusing instead would make the
        documented in-memory mode unusable; degrading to a slower read of the
        same rule cannot give a wrong answer, and the run path's wiring is
        pinned by a test that counts transcript reads rather than left to
        whoever next builds one of these.
        """
        self._load_transcript = load_transcript
        self._append_context_event = append_context_event
        self._load_tags = load_tags
        self._latest_event_of_type = latest_event_of_type

    def origin_for(
        self, session_id: str, context_payload: dict[str, object] | None
    ) -> SessionOrigin:
        """Classify a session's purpose from its TAGS as well as its context.

        Tags win over context (the classifier's own rule), so this reads them
        rather than classifying from the payload alone — a wiki/search/apps
        surface tags at creation and may write no capability at all, and the
        capabilities a client DOES advertise describe what it can render, not
        what the session is for. Classifying context-only therefore files a real
        Apps or wiki session under ``user``, which un-binds it: ``purpose_bound``
        goes false and the whole ``OVERRIDABLE_WHEN_UNBOUND`` tier re-opens.
        """
        tags: list[str] = []
        if self._load_tags is not None:
            try:
                tags = [str(tag) for tag in self._load_tags(session_id)]
            except Exception:  # noqa: BLE001 - a tag read must never brick a session load
                tags = []
        return SessionOrigin.classify(tags, context_payload or {})

    def load(self, session_id: str) -> SessionSpec:
        """The session's binding: the newest typed mirror, else reconstruction from loose keys.

        ``O(1)`` on the Mongo driver, ``O(one session)`` on the base store or when no
        type-bounded reader was injected. This is the hottest read in the class — every
        ``/query``, ``/message``, ``/recover`` and unattended trigger fire loads the
        binding first — so it asks for the ONE context event it needs rather than a
        transcript. Measured on the deployed store's largest session: 10,296 documents /
        14.5 MB against 1 document / 0.2 KB, with ``explain`` reporting
        ``docsExamined 1`` off the existing ``ix_events_type_session_ts``.

        Two reads, and the second only fires when the first misses: the newest context
        event CARRYING the mirror, then the newest context event at all. The narrowing is
        by TYPE, never by a count — the newest context event sits arbitrarily far back
        after a long run, and a window that misses it would report "no binding", which
        un-binds a purpose-bound session and re-opens the whole
        ``OVERRIDABLE_WHEN_UNBOUND`` tier. That is a wrong ANSWER, not a slow one.

        The fallback deliberately reads ONE payload rather than merging every context
        event: merging would resurrect a field the user cleared (a removed project
        sticking forever), the same reason the generic context reader never merges
        either.

        The typed mirror's ``origin`` is RE-CLASSIFIED here too, through the same
        :meth:`origin_for` the reconstruction legs use, rather than trusted verbatim
        off the blob. ``SessionOrigin`` is derived, never stored — a blob's ``origin``
        field is only ever a value some earlier classifier stamped at write time, and
        trusting it verbatim would freeze a session under a stale classification the
        NEXT classifier fix corrects for every session reconstructing from context but
        not for this one. Every other field on the mirror stays authoritative; only
        this one field is re-derived on every read, like the rest of the binding.

        A value under ``SPEC_CONTEXT_KEY`` that is present but NOT a dict reconstructs
        from the payload carrying it. Nothing writes such a value —
        :meth:`SessionSpec.to_context_payload` always stores a dict — but the narrowing
        the store applies is "the key is SET", deliberately coarser than "the key holds
        a blob"; a store cannot know what a caller's key means, so deciding what a
        non-blob value means belongs here.
        """
        payload = self._payload_of(
            self._latest_context(session_id, payload_key=SPEC_CONTEXT_KEY)
        )
        if payload is None:
            return self._reconstruct(
                session_id, self._payload_of(self._latest_context(session_id))
            )
        blob = payload.get(SPEC_CONTEXT_KEY)
        if isinstance(blob, dict):
            try:
                return SessionSpec.from_blob(
                    blob, origin=self.origin_for(session_id, payload)
                )
            except Exception:  # noqa: BLE001 - a stored spec must never brick its session
                return self._reconstruct(session_id, payload)
        return self._reconstruct(session_id, payload)

    def has_typed_mirror(self, session_id: str) -> bool:
        """Whether a durable binding was RECORDED, vs one reconstructed from context.

        ``O(1)`` on the Mongo driver, ``O(one session)`` otherwise — the same
        bounded read :meth:`load` opens with, so the projection's ``source`` field
        and the binding it describes can never disagree about whether a mirror
        exists. It lives here rather than beside the route because it reads state
        this store owns, and because a route asking the question its own way is
        how the two came to answer from different scans.

        "Any context event carries a mirror" and "the NEWEST context event
        carrying a mirror exists" are the same question — a bounded read answers
        it without materialising the transcript to prove a single existence.
        """
        return self._latest_context(session_id, payload_key=SPEC_CONTEXT_KEY) is not None

    def save(
        self, session_id: str, spec: SessionSpec, *, capabilities: Sequence[str] | None = None
    ) -> None:
        """Persist *spec* as a full context event (typed mirror + loose keys)."""
        self._append_context_event(session_id, spec.to_context_payload(capabilities=capabilities))

    def _reconstruct(self, session_id: str, payload: dict[str, object] | None) -> SessionSpec:
        """Rebuild a spec-less session's binding, classified from tags + loose context keys.

        Strips the typed-mirror key even when present: the corrupt-blob leg of
        :meth:`load` calls this with the SAME payload whose ``SPEC_CONTEXT_KEY``
        blob just failed to parse, and ``SessionSpec.from_context`` re-checks that
        key and re-enters ``from_blob`` on it otherwise — the same parse, the same
        exception, this time uncaught. Excluding the key here is what makes the
        fallback actually take the loose-key path rather than re-failing the parse
        it exists to recover from.
        """
        loose = {
            key: value for key, value in (payload or {}).items() if key != SPEC_CONTEXT_KEY
        }
        return SessionSpec.from_context(loose, origin=self.origin_for(session_id, payload))

    def _latest_context(
        self, session_id: str, *, payload_key: str | None = None
    ) -> dict[str, Any] | None:
        """The newest ``context`` event, optionally one whose payload sets *payload_key*.

        ``O(1)`` through an injected store reader, ``O(one session)`` through the
        transcript fallback. ONE seam so :meth:`load` is written once against one
        question — a second copy of the binding's selection rule, one per source,
        is how the fast path and the degraded path come to disagree about which
        event IS the binding.

        The fallback restates nothing: it walks newest-first and defers the
        "is this key set" judgement to ``SessionStoreBase.payload_key_is_set``,
        the published spelling both store drivers already implement, so all
        three readers narrow identically.

        Degrades to ``None`` rather than raising — an unreadable transcript means
        "no binding yet", which is the same contract the transcript read it
        replaces already had on this path.
        """
        if self._latest_event_of_type is not None:
            try:
                return self._latest_event_of_type(session_id, "context", payload_key)
            except Exception:  # noqa: BLE001 - an unreadable store means "no binding yet"
                return None
        try:
            events = self._load_transcript(session_id)
        except Exception:  # noqa: BLE001 - an unreadable transcript means "no binding yet"
            return None
        for event in reversed(events):
            if event.get("type") != "context":
                continue
            if payload_key is not None and not SessionStoreBase.payload_key_is_set(
                event, payload_key
            ):
                continue
            return event
        return None

    @staticmethod
    def _payload_of(event: dict[str, Any] | None) -> dict[str, object] | None:
        """An event's payload when it is a usable mapping, else ``None``.

        A payload that is not a dict carries no binding and must not be handed to
        :meth:`_reconstruct` as if it did — ``None`` is what "this session has no
        context to rebuild from" has always looked like here.
        """
        if event is None:
            return None
        payload = event.get("payload")
        return payload if isinstance(payload, dict) else None
