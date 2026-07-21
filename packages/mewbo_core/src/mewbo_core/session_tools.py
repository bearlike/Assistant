#!/usr/bin/env python3
"""Session-scoped tools: per-agent stateful handlers contributed by plugins.

Unlike stateless tools in ToolRegistry, a ``SessionTool`` is constructed
per agent instance with a session id and an event logger. It declares its
own OpenAI function schema, handles the tool call directly, and can
signal clean loop termination (same pattern ``exit_plan_mode`` has used
since day one).

Plugins contribute session tools via a ``session_tools:`` array in
their ``plugin.json``. At session start the orchestrator imports each
entry's Python class and registers it as a factory in this module's
``SessionToolRegistry``. Each ``ToolUseLoop``, given the agent's
``allowed_tools``, builds the subset of factories whose ``tool_id``
matches — producing one ``SessionTool`` instance per agent per session.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.tool_registry import capability_mode_admits
from mewbo_core.types import Event

logging = get_logger(name="core.session_tools")


@runtime_checkable
class SessionTool(Protocol):
    """Per-agent stateful tool handler — schema, dispatch, termination.

    ``modes`` declares the orchestration modes the tool is valid in
    (a frozenset of ``"plan"`` / ``"act"``). Plugin tools default to
    act-mode only via :data:`DEFAULT_SESSION_TOOL_MODES`; core's
    ``ExitPlanModeTool`` overrides to ``{"plan"}``.

    **Optional class attributes**, read by the loop via ``getattr`` and
    deliberately NOT declared as protocol members. A non-method protocol member
    is REQUIRED for structural conformance — a default value in this body does
    not relax that (verified against the checker; a ``property`` does not
    either) — so declaring one here would break every standalone implementer
    that has no reason to care about it. They are a convention with a default,
    not part of the contract:

    ``max_result_chars`` — the tool's model-facing result cap. It matters
    because a session tool is absent from the stateless ``ToolRegistry``, so
    ``ToolRegistry.get_spec`` returns ``None`` for it and the loop's truncation
    seam fell through to the registry default of 2000: a number calibrated for
    unbounded shell/MCP output and never chosen for these tools. The cost was
    silent and total — a repo-manifest scan delivered 22 of ~1,900 paths while
    the playbook's next step required choosing files from that manifest, and a
    page read exceeded the cap on 100% of its successful calls. Omit it to take
    :data:`DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS`; set it to declare a smaller
    (or larger) ceiling.

    ``poll_class`` / ``poll_when_args`` — declare that a call is POLLING rather
    than progress, so the no-progress guard does not read honest waiting as a
    stuck loop. ``poll_class = True`` marks every call to the tool as a poll;
    ``poll_when_args = ("run_id",)`` marks only calls carrying one of those
    arguments, which is what a tool that both STARTS and POLLS a run needs —
    the two are one tool id distinguishable only by argument shape.
    """

    tool_id: str
    schema: dict[str, object]
    modes: frozenset[str]

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute the tool call and return a speaker-style result."""
        ...

    def should_terminate_run(self) -> bool:
        """Return True (consuming the flag) when the loop should exit cleanly."""
        ...

    def terminal_reason(self) -> str:
        """Return the ``done_reason`` to stamp when this tool terminates the run.

        The default is ``"awaiting_approval"`` (the ``exit_plan_mode`` pattern).
        Tools that signal a *successful* terminal state (e.g.
        ``EmitStructuredResponseTool``) override this to ``"completed"`` so the
        loop stamps the right reason without a hardcoded literal.
        """
        return "awaiting_approval"


# Plugin session tools default to act-mode-only. A plugin that wants its
# tool bound in plan mode must override ``modes`` on the class.
DEFAULT_SESSION_TOOL_MODES: frozenset[str] = frozenset({"act"})

# Model-facing result cap for a session tool that declares none of its own.
#
# Sized to the payload a session tool actually returns: a curated, first-party,
# structured result (a repo manifest, a wiki page, a graph slice), not the
# unbounded and untrusted output the registry's 2000 default guards against.
#
# The number comes from measuring the artifact rather than from taste. A real
# repository of the size this harness indexes lists 1,934 files in 99,380
# characters (mean path 50). A cap AT that size would clip the next repo, so
# this carries roughly twice it — about 4,000 files at the same mean — and a
# manifest that outgrows even this wants a paged handle, not a bigger number.
#
# A tool whose payload is genuinely small should still declare its own smaller
# cap: this is the floor for the undeclared case, not a target to spend.
DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS: int = 200_000


EventLogger = Callable[[Event], None]
SessionToolBuilder = Callable[[str, EventLogger | None], SessionTool]


@dataclass(frozen=True)
class SessionToolFactory:
    """Builds one ``SessionTool`` instance for a given session.

    ``requires_capabilities`` is the plugin-manifest capability gate (e.g. the
    ``scg`` suite's ``["scg"]``). When non-empty AND a subset of the session's
    capabilities, :meth:`SessionToolRegistry.build_for` instantiates the tool
    even if it is absent from the agent's ``allowed_tools`` — so a capability
    granted at RUNTIME (not a client advertisement) surfaces
    its session tools to the root agent. That auto-surface is bounded by the
    STRUCTURAL CEILING: it fires only when the caller gave NO
    explicit ``allowed_tools`` (``None``) — ANY list caps the capability gate,
    an empty one included, since ``[]`` is an explicit grant of nothing.
    An empty tuple keeps the historical allowlist-only behaviour (the tool
    appears only when explicitly allowed).

    ``unconditional`` marks an always-on-by-default session tool: it surfaces to
    ANY session that declares no explicit tool scope, with NO capability
    required (``schedule_trigger``, which had this shape as an
    ``extra_session_tools`` injection but was thereby structurally unreachable by
    spawned sub-agents). A plugin declares it PER ENTRY via
    :class:`SessionToolEntry`, so a bundle that is capability-gated as a whole
    can still ship individual default-on tools. It respects the SAME structural ceiling as the
    capability gate: an explicit ``allowed_tools`` caps it, so a scoped
    sub-agent still admits the tool ONLY by naming it in its allowlist — which is
    exactly how the app-builder AgentDef's ``tools:`` list now delivers it.
    """

    tool_id: str
    build: SessionToolBuilder
    requires_capabilities: tuple[str, ...] = ()
    unconditional: bool = False
    # Declared privilege tier for delegation ``capability_mode`` filtering.
    # ``None`` → the session-tool DEFAULT of ``execute``
    # (see :meth:`capability_tier`): a session tool is a session ACTION —
    # submit/mint/commit/arm are writes by nature — so under a ``read_only``
    # spawn NO session tool survives unless it explicitly declares ``read``.
    capability: Literal["read", "write", "execute"] | None = None

    def capability_tier(self) -> str:
        """Resolve this factory's privilege tier for ``capability_mode`` filtering.

        Defaults to ``"execute"`` (session tools are actions/writes), so
        ``read_only`` admits only a factory that explicitly declares ``"read"``
        — with nothing annotated today that is ZERO session tools, the correct
        safe-deny. ``execute`` and ``all`` admit the default-``execute`` tier, so
        they are byte-identical to the historical behaviour.
        """
        return self.capability or "execute"


class SessionToolEntry(BaseModel):
    """One ``session_tools`` record from a plugin manifest.

    A manifest is an on-disk file authored outside the engine, so the record is
    validated HERE rather than read field-by-field at the load site.
    ``extra="forbid"`` is what makes that worth doing: a misspelt
    ``unconditional`` would otherwise be dropped in silence, leaving the tool
    behind its bundle's capability gate with no signal that the author asked for
    anything else.
    """

    model_config = ConfigDict(extra="forbid")

    tool_id: str
    module: str
    class_name: str = Field(alias="class")
    unconditional: bool = Field(
        default=False,
        description=(
            "Surface this tool to any session that declares no STRICT tool "
            "scope, with NO capability required. Absent (the default) keeps the "
            "entry behind its bundle's ``requires-capabilities`` gate, so an "
            "existing manifest is unaffected."
        ),
    )

    @field_validator("tool_id", "module", "class_name")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """Reject a blank identifier — an empty ``tool_id`` registers an unreachable factory."""
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class SessionToolRegistry:
    """Registry of session-tool factories — populated once per session.

    Construction is cheap: a plugin-manifest entry ``{"tool_id", "module",
    "class"}`` is imported and turned into a factory that feeds the class
    ``(session_id=, event_logger=)`` on call.
    """

    def __init__(self) -> None:
        """Create an empty registry."""
        self._factories: dict[str, SessionToolFactory] = {}

    def register(self, factory: SessionToolFactory) -> None:
        """Register *factory*. First registration wins — no override."""
        self._factories.setdefault(factory.tool_id, factory)

    def load_entry(
        self,
        entry: dict[str, object],
        *,
        requires_capabilities: tuple[str, ...] = (),
    ) -> None:
        """Import a :class:`SessionToolEntry` record and register it.

        *requires_capabilities* is the contributing plugin's manifest gate
        (``pc.manifest.requires_capabilities``); it stamps the factory so
        :meth:`build_for` can surface the tool to any session that holds the
        capability — even via a runtime grant rather than an explicit allowlist
        entry. Empty (the default) preserves allowlist-only visibility.

        An entry may opt OUT of that bundle-wide gate with ``"unconditional":
        true``, which stamps :attr:`SessionToolFactory.unconditional` — the
        per-entry escape hatch for a tool whose bundle is capability-gated as a
        whole but which is safe by default. Note this is NOT expressible as an
        empty ``requires_capabilities``: :meth:`ids_for` only auto-selects a
        NON-empty gate that the session satisfies, so an empty one means
        "allowlist-only", the opposite of default-on.

        Malformed entries (missing/blank fields, unknown keys, import errors)
        are logged and skipped — a broken plugin must not crash the host.
        """
        try:
            record = SessionToolEntry.model_validate(entry)
        except ValidationError as exc:
            logging.warning("invalid session_tools entry {}: {}", entry, exc)
            return
        try:
            module = importlib.import_module(record.module)
            cls = getattr(module, record.class_name)
        except (ImportError, AttributeError) as exc:
            logging.warning(
                "Failed to import session tool {}.{}: {}",
                record.module,
                record.class_name,
                exc,
            )
            return

        def _build(session_id: str, event_logger: EventLogger | None) -> SessionTool:
            return cls(session_id=session_id, event_logger=event_logger)

        self.register(
            SessionToolFactory(
                tool_id=record.tool_id,
                build=_build,
                requires_capabilities=requires_capabilities,
                unconditional=record.unconditional,
            )
        )

    def build_for(
        self,
        allowed_tools: list[str] | None,
        *,
        session_id: str,
        event_logger: EventLogger | None,
        session_capabilities: tuple[str, ...] = (),
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
    ) -> list[SessionTool]:
        """Instantiate every matching tool for the given agent.

        Three gates select which factories build, deduped by id:

        1. **Allowlist** — any id present in *allowed_tools* (the historical
           per-agent scope, e.g. a sub-agent's ``allowed_tools`` from its
           AgentDef, or a workspace-bound run's connector grant).
        2. **Capability** — any factory whose ``requires_capabilities`` is
           non-empty AND a subset of *session_capabilities*. This is the bridge
           for a RUNTIME-granted capability: the ``scg`` provider
           unions ``scg`` into the session caps, so the root agent of an
           ordinary session gets the ``scg_*`` tools without the client ever
           listing them in ``allowed_tools``. The gate stays data-driven —
           ``requires_capabilities`` flows from the plugin manifest, no tool id
           is hardcoded here.
        3. **Unconditional** — any factory marked ``unconditional`` (``schedule_trigger``):
           an always-on-by-default tool that surfaces
           UNLESS a STRICT explicit scope caps it (see below).

        **Two different ceilings, because two different kinds of allowlist.**
        The capability gate (2) uses the structural ceiling: any non-empty
        *allowed_tools* caps it. The unconditional gate (3) uses the df875 law
        (`strict_tool_scope`): a non-empty allowlist caps it ONLY when
        *strict_tool_scope* is True. This distinction is load-bearing — the
        console/Aura FE ALWAYS sends a large ``context.mcp_tools`` list
        (permissive: ``strict_tool_scope=False``), which is a ceiling over MCP
        tools ONLY and never lists built-in session tools. Treating that
        permissive allowlist as a ceiling on ``schedule_trigger`` silently broke
        every mobile alarm/reminder flow (the FE root arms a time trigger via
        ``schedule_trigger``). A STRICT scope (an AgentDef ``tools:`` — the
        app-builder, the wiki-qa hypervisor) IS authoritative for everything, so
        there the unconditional tool must be NAMED to appear — exactly as it must
        for a capability tool. Named-in-allowlist (gate 1) always wins regardless.

        **Delegation privilege ceiling.** *capability_mode* is a coarse
        ceiling applied AFTER the three gates, mirroring the registry-tool filter
        (:func:`filter_specs`) so both tool surfaces attenuate together (the
        two-surface trap). Session tools default to tier ``execute`` (they are
        session actions/writes), so ``read_only`` admits ONLY a factory that
        explicitly declares ``read`` — zero today, the correct safe-deny — while
        ``execute``/``all`` are byte-identical to before. It cannot resurrect a
        tool the gates dropped (it only removes more).

        Returns an empty list when no gate selects anything. A factory that
        raises during instantiation (e.g. a plugin tool whose ``__init__``
        signature is wrong) is logged and skipped — a broken plugin must never
        abort session startup.
        """
        tools: list[SessionTool] = []
        for tid in self.ids_for(
            allowed_tools,
            session_capabilities=session_capabilities,
            strict_tool_scope=strict_tool_scope,
            capability_mode=capability_mode,
        ):
            factory = self._factories[tid]
            try:
                tools.append(factory.build(session_id, event_logger))
            except Exception as exc:  # noqa: BLE001 — broken plugin must not kill session
                logging.warning(
                    "session tool {} failed to instantiate: {}", tid, exc
                )
        return tools

    def ids_for(
        self,
        allowed_tools: list[str] | None,
        *,
        session_capabilities: tuple[str, ...] = (),
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
    ) -> list[str]:
        """The ids :meth:`build_for` would select, WITHOUT instantiating anything.

        The three gates (allowlist, then the capability + unconditional
        auto-surfaces, each with its own allowlist ceiling — see
        :meth:`build_for`) live here and here only — :meth:`build_for` calls this
        and then builds. That shared algorithm is the point: a caller that needs
        to NAME an agent's session tools (the operator-facing
        ``InstructionContext.tools``, which must not lie about what the agent
        holds) can never drift from the set that actually gets built — so callers
        of BOTH must pass the SAME *strict_tool_scope* AND *capability_mode*.

        *capability_mode* is the delegation privilege ceiling applied over
        the selected set (see :meth:`build_for`); ``all`` (the default) is a no-op.

        Pure lookup, no I/O, no side effects — safe to call before the tools
        exist.
        """
        caps = set(session_capabilities)
        # Three-state: ``None`` is unrestricted, ``[]`` is an explicit grant of
        # nothing — both distinct from a non-empty ceiling. Truthiness would read
        # the empty grant as "no scope declared" and auto-surface every
        # capability/unconditional tool the empty allowlist exists to withhold.
        has_explicit_scope = allowed_tools is not None
        selected: list[str] = []
        seen: set[str] = set()
        for tid in allowed_tools or []:
            if tid in self._factories and tid not in seen:
                selected.append(tid)
                seen.add(tid)
        for tid, factory in self._factories.items():
            if tid in seen:
                continue
            if factory.unconditional:
                # Always-on-by-default (schedule_trigger), no capability
                # required. df875 law: a non-empty allowlist caps it ONLY under
                # STRICT scope (an authoritative AgentDef ``tools:``). A
                # PERMISSIVE allowlist — the FE default, where
                # ``context.mcp_tools`` is an MCP-tool ceiling that never lists
                # built-ins — must still surface it, or every console/Aura root
                # (always a permissive session with a non-empty mcp_tools list)
                # loses schedule_trigger and the mobile alarm/reminder flow
                # breaks. Named-in-allowlist already handled by the loop above.
                if strict_tool_scope and has_explicit_scope:
                    continue
                selected.append(tid)
                seen.add(tid)
                continue
            if has_explicit_scope:
                # The capability gate keeps the structural ceiling: ANY
                # explicit allowlist (strict or permissive, INCLUDING an empty
                # one) caps it. Preserved for a plain session — ``allowed_tools``
                # absent, i.e. ``None`` — only.
                continue
            req = factory.requires_capabilities
            if req and set(req).issubset(caps):
                selected.append(tid)
                seen.add(tid)
        # Delegation privilege ceiling: apply the capability_mode gate to
        # the FINAL selection, regardless of WHICH gate admitted each id — so a
        # read_only spawn can never resurrect a write-tier session tool by naming
        # it. ``all`` short-circuits (byte-identical). Session tools default to
        # tier ``execute`` (``SessionToolFactory.capability_tier``), so read_only
        # drops every one that has not explicitly declared ``read``.
        if capability_mode != "all":
            selected = [
                tid
                for tid in selected
                if capability_mode_admits(
                    capability_mode, self._factories[tid].capability_tier()
                )
            ]
        return selected

    def capabilities_for(self, tool_ids: Iterable[str]) -> tuple[str, ...]:
        """Union ``requires_capabilities`` of every registered factory in *tool_ids*.

        Pure lookup, no I/O — the read-side mirror of the capability half of
        :meth:`build_for`'s gate. Used to derive a REQUEST-SCOPED capability
        grant from a caller's tool allowlist: selecting a
        product tool by id (e.g. via ``context.mcp_tools``) also unlocks the
        capability that gates its AgentDef family on the catalog surface,
        without a separate client-advertised header. Unknown ids and
        factories with no capability gate contribute nothing. Returns a
        sorted, deduped tuple (same shape as :func:`capabilities.parse_capabilities`).
        """
        granted: set[str] = set()
        for tid in tool_ids:
            factory = self._factories.get(tid)
            if factory is not None:
                granted.update(factory.requires_capabilities)
        return tuple(sorted(granted))


__all__ = [
    "DEFAULT_SESSION_TOOL_MAX_RESULT_CHARS",
    "DEFAULT_SESSION_TOOL_MODES",
    "SessionTool",
    "SessionToolEntry",
    "SessionToolFactory",
    "SessionToolRegistry",
]
