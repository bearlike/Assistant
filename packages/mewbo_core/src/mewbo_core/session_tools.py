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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.types import Event

logging = get_logger(name="core.session_tools")


@runtime_checkable
class SessionTool(Protocol):
    """Per-agent stateful tool handler — schema, dispatch, termination.

    ``modes`` declares the orchestration modes the tool is valid in
    (a frozenset of ``"plan"`` / ``"act"``). Plugin tools default to
    act-mode only via :data:`DEFAULT_SESSION_TOOL_MODES`; core's
    ``ExitPlanModeTool`` overrides to ``{"plan"}``.
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


EventLogger = Callable[[Event], None]
SessionToolBuilder = Callable[[str, EventLogger | None], SessionTool]


@dataclass(frozen=True)
class SessionToolFactory:
    """Builds one ``SessionTool`` instance for a given session.

    ``requires_capabilities`` is the plugin-manifest capability gate (e.g. the
    ``scg`` suite's ``["scg"]``). When non-empty AND a subset of the session's
    capabilities, :meth:`SessionToolRegistry.build_for` instantiates the tool
    even if it is absent from the agent's ``allowed_tools`` — so a capability
    granted at RUNTIME (the #83-B provider, not a client advertisement) surfaces
    its session tools to the root agent. An empty tuple keeps the historical
    allowlist-only behaviour (the tool appears only when explicitly allowed).
    """

    tool_id: str
    build: SessionToolBuilder
    requires_capabilities: tuple[str, ...] = ()


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
        """Import a ``{tool_id, module, class}`` record and register it.

        *requires_capabilities* is the contributing plugin's manifest gate
        (``pc.manifest.requires_capabilities``); it stamps the factory so
        :meth:`build_for` can surface the tool to any session that holds the
        capability — even via a runtime grant rather than an explicit allowlist
        entry. Empty (the default) preserves allowlist-only visibility.

        Malformed entries (missing fields, import errors) are logged and
        skipped — a broken plugin must not crash the host.
        """
        tool_id = str(entry.get("tool_id", "")).strip()
        module_name = str(entry.get("module", "")).strip()
        class_name = str(entry.get("class", "")).strip()
        if not (tool_id and module_name and class_name):
            logging.warning(
                "session_tools entry missing required fields: {}", entry
            )
            return
        try:
            module = importlib.import_module(module_name)
            cls = getattr(module, class_name)
        except (ImportError, AttributeError) as exc:
            logging.warning(
                "Failed to import session tool {}.{}: {}",
                module_name,
                class_name,
                exc,
            )
            return

        def _build(session_id: str, event_logger: EventLogger | None) -> SessionTool:
            return cls(session_id=session_id, event_logger=event_logger)

        self.register(
            SessionToolFactory(
                tool_id=tool_id,
                build=_build,
                requires_capabilities=requires_capabilities,
            )
        )

    def build_for(
        self,
        allowed_tools: list[str] | None,
        *,
        session_id: str,
        event_logger: EventLogger | None,
        session_capabilities: tuple[str, ...] = (),
    ) -> list[SessionTool]:
        """Instantiate every matching tool for the given agent.

        Two gates select which factories build, unioned and deduped by id:

        1. **Allowlist** — any id present in *allowed_tools* (the historical
           per-agent scope, e.g. a sub-agent's ``allowed_tools`` from its
           AgentDef, or a workspace-bound run's connector grant).
        2. **Capability** — any factory whose ``requires_capabilities`` is
           non-empty AND a subset of *session_capabilities*. This is the bridge
           for a RUNTIME-granted capability (#83-B / #84): the ``scg`` provider
           unions ``scg`` into the session caps, so the root agent of an
           ordinary session gets the ``scg_*`` tools without the client ever
           listing them in ``allowed_tools``. The gate stays data-driven —
           ``requires_capabilities`` flows from the plugin manifest, no tool id
           is hardcoded here.

        Returns an empty list when neither gate selects anything. A factory that
        raises during instantiation (e.g. a plugin tool whose ``__init__``
        signature is wrong) is logged and skipped — a broken plugin must never
        abort session startup.
        """
        caps = set(session_capabilities)
        selected: list[str] = []
        seen: set[str] = set()
        for tid in allowed_tools or []:
            if tid in self._factories and tid not in seen:
                selected.append(tid)
                seen.add(tid)
        for tid, factory in self._factories.items():
            if tid in seen:
                continue
            req = factory.requires_capabilities
            if req and set(req).issubset(caps):
                selected.append(tid)
                seen.add(tid)
        tools: list[SessionTool] = []
        for tid in selected:
            factory = self._factories[tid]
            try:
                tools.append(factory.build(session_id, event_logger))
            except Exception as exc:  # noqa: BLE001 — broken plugin must not kill session
                logging.warning(
                    "session tool {} failed to instantiate: {}", tid, exc
                )
        return tools


__all__ = [
    "DEFAULT_SESSION_TOOL_MODES",
    "SessionTool",
    "SessionToolFactory",
    "SessionToolRegistry",
]
