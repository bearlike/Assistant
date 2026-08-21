#!/usr/bin/env python3
"""Session orchestration entrypoint."""

from __future__ import annotations

import asyncio
import platform as _platform
import queue
import socket
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from mewbo_core.agents.agent_context import AgentContext
from mewbo_core.agents.hypervisor import ACTIVE_STATUSES, AgentHypervisor
from mewbo_core.classes import (
    UNACHIEVED_DONE_REASONS,
    ActionStep,
    OrchestrationState,
    Plan,
    TaskQueue,
)
from mewbo_core.common import discover_project_instructions, get_logger, session_log_context
from mewbo_core.components import langfuse_session_context
from mewbo_core.config import (
    PluginsConfig,
    effective_fallback_models,
    get_config,
    get_config_value,
    get_version,
)
from mewbo_core.contracts.run_error import RunError
from mewbo_core.contracts.types import CompletionPayload
from mewbo_core.hooks import HookManager, default_hook_manager
from mewbo_core.loop.tool_use_loop import ToolUseLoop
from mewbo_core.permissions import (
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from mewbo_core.safety.plane import SafetyPlane
from mewbo_core.session.context import ContextBuilder
from mewbo_core.session.session_provenance import SessionOrigin, TraceProvenance, is_mobile_surface
from mewbo_core.session.session_store import SessionStoreBase, create_session_store
from mewbo_core.session.token_budget import get_token_budget
from mewbo_core.system_instructions import (
    InstructionContext,
    SystemInstructionsStoreBase,
    create_system_instructions_store,
)
from mewbo_core.tooling.exit_plan_mode import ensure_plan_dir, plan_file_for
from mewbo_core.tooling.session_tools import SessionTool, SessionToolRegistry
from mewbo_core.tooling.skills import SkillRegistry, activate_skill
from mewbo_core.tooling.tool_registry import (
    ToolRegistry,
    ToolSpec,
    filter_specs,
    get_or_build_registry,
)

if TYPE_CHECKING:
    # Annotation-only: the catalog and its two stores are imported lazily inside
    # ``_build_project_catalog``, so an ordinary run never loads them.
    from mewbo_core.workspaces.project_catalog import ProjectCatalog

logging = get_logger(name="core.orchestrator")

# Longest error blurb to embed in a synthetic closure event. Keeps the
# transcript readable and the downstream ``recent_events`` bullet from
# ballooning the system prompt.
_CLOSURE_ERROR_MAX_LEN = 500

# First-person commitments to future work, matched in terminal prose. The list
# is short and literal ON PURPOSE. This is the one heuristic in the honest-
# terminal workstream, and the only thing a heuristic can be trusted with here
# is stating a fact the record already proves: a general intent classifier
# firing on ambiguous prose would manufacture precisely the kind of
# unfalsifiable claim this seam exists to remove. Anything broader (a bare
# mention of "in the background") describes work that may genuinely exist and
# is deliberately not matched.
_FUTURE_COMMITMENT_MARKERS: tuple[str, ...] = (
    "i'll check back",
    "i will check back",
    "i'll follow up",
    "i will follow up",
    "i'll report back",
    "i will report back",
    "i'll keep monitoring",
    "i will keep monitoring",
    "i'll keep an eye",
    "i will keep an eye",
)


def _format_assistant_closure(done_reason: str | None, last_error: str | None) -> str:
    """Return a short human-readable closure marker for a terminal run.

    Emitted when a run ends without a real ``task_result`` so every user
    turn has exactly one materialised assistant event in the transcript.
    Frontend ``buildTimeline`` relies on this to finalise turn metadata,
    and the LLM's ``recent_events`` bullet list gains a narrative
    closure for recovery runs.
    """
    if done_reason == "error":
        err = (last_error or "unknown error").strip()
        if len(err) > _CLOSURE_ERROR_MAX_LEN:
            err = err[:_CLOSURE_ERROR_MAX_LEN] + "…"
        return f"(Run interrupted by error: {err})"
    if done_reason == "max_steps_reached":
        return "(Run stopped: step limit reached before final answer)"
    if done_reason == "budget_exhausted":
        return "(Run stopped: step budget exhausted before final answer)"
    if done_reason == "canceled":
        return "(Run canceled by user)"
    return f"(Run ended: {done_reason or 'unknown'})"


class Orchestrator:
    """Unified tool-use orchestration loop."""

    #: The model the loop was last generating with. ``None`` until a loop has
    #: run, which is the honest answer for a failure raised before one did.
    #: Kept because ``_model_name`` is frozen at construction and a run that
    #: escalates down the ladder leaves it naming a model that served nothing —
    #: the failure record is what an operator benches a model from. Declared on
    #: the CLASS so the emission rule stays drivable from a bare instance, which
    #: is how its tests reach it without paying for a whole orchestrator.
    _served_model: str | None = None

    def __init__(
        self,
        *,
        model_name: str | None = None,
        fallback_models: tuple[str, ...] | None = None,
        session_store: SessionStoreBase | None = None,
        tool_registry: ToolRegistry | None = None,
        permission_policy: PermissionPolicy | None = None,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager | None = None,
        cwd: str | None = None,
        session_step_budget: int = 0,
        system_instructions_store: SystemInstructionsStoreBase | None = None,
        session_mcp_servers: dict[str, dict] | None = None,
    ) -> None:
        """Initialize orchestration dependencies.

        *session_mcp_servers* are MCP servers a caller attaches to THIS run, in
        the standard ``{name: {command|url, …}}`` config shape. They are merged
        into the registry build below alongside the plugin-contributed ones, and
        their discovered tool ids are admitted through this run's
        ``allowed_tools`` (:meth:`_session_mcp_tool_ids`).

        **It is deliberately NOT named ``extra_mcp_servers``, even though that is
        the parameter it feeds.** The two carry different grants, and reusing one
        name for both senses is the trap the house rules name outright: a
        plugin-contributed server's tools are subject to ``allowed_tools`` like
        any other registry tool, while a server named HERE is admitted by the act
        of naming it — the caller attaching a server for one run IS the grant, and
        it has no other way to express one, since a server's tool ids are not
        knowable until discovery has run. Widening the existing parameter's
        meaning instead would silently admit every plugin server's tools into
        every scoped session in the deployment.

        Discovery is a network/subprocess cost and it belongs HERE rather than at
        whichever caller assembled the config: this constructor already runs on
        the background run thread, which is the offline side of the
        acceptance/execution boundary. A caller resolving the ids itself would be
        paying for discovery on its own request path.
        """
        self._cwd = cwd
        self._session_mcp_servers = dict(session_mcp_servers or {})
        self._session_step_budget = session_step_budget
        # Resolved LAZILY on first use (``_instructions_store``): the factory
        # raises when the configured driver is mongodb and Mongo is unreachable,
        # and a custom-instructions store being down must never stop a session
        # from starting. ``_instructions_store_tried`` makes the failure sticky
        # so we don't retry a dead connection once per run.
        self._instructions_store: SystemInstructionsStoreBase | None = system_instructions_store
        self._instructions_store_tried = system_instructions_store is not None
        # Strong references for fire-and-forget background tasks scheduled on
        # an already-running loop (the emscripten branch of
        # ``_maybe_generate_title``) so asyncio can't GC them mid-run — the
        # done-callback discards its own entry once finished. Mirrors
        # ``AgentHandle.asyncio_task`` (hypervisor.py) / ``_lifecycle_tasks``
        # (spawn_agent.py).
        self._background_tasks: set[asyncio.Task] = set()
        self._model_name = (
            model_name
            or get_config_value("llm", "action_plan_model")
            or get_config_value("llm", "default_model", default="gpt-5.2")
        )
        self._fallback_models = (
            fallback_models
            if fallback_models is not None
            else tuple(effective_fallback_models())
        )
        self._session_store = session_store or create_session_store()
        self._permission_policy = permission_policy or load_permission_policy()
        self._approval_callback = approval_callback or approval_callback_from_config()
        self._hook_manager = hook_manager or default_hook_manager()

        self._project_instructions = discover_project_instructions(cwd)
        # Loaded exactly once, here, from operator-owned config — never from a
        # tool a model can call. ``None`` when the deployment-wide switch is off
        # (the default): every call site below tests ``is not None`` and does
        # nothing else, so an unconfigured install never stats ``.mewbo/``,
        # never parses a document and never builds a rule.
        self._safety_plane = SafetyPlane.load(
            cwd, enabled=bool(get_config_value("safety", "enabled", default=False))
        )
        self._skill_registry = SkillRegistry()
        self._skill_registry.load(cwd)

        # Plugin loading (before load_registry so MCP servers are collected first).
        # Uses the shared load_all_plugin_components() so the same logic is
        # reused by the API /skills and /tools endpoints (DRY).
        self._agent_registry = None
        self._session_tool_registry = SessionToolRegistry()
        # schedule_trigger rides the ordinary SessionToolRegistry so
        # a spawned sub-agent whose allowlist admits it can bind it — the old
        # root-only extra_session_tools seam structurally could not. The factory
        # exists only once the app pushed its store+policy; None (CLI/tests) ⇒
        # the tool is simply absent, exactly as before.
        from mewbo_core.triggers.session_tool import schedule_trigger_factory

        _trigger_factory = schedule_trigger_factory()
        if _trigger_factory is not None:
            self._session_tool_registry.register(_trigger_factory)
        plugins_cfg = get_config().plugins

        if plugins_cfg.enabled:
            # Reconcile missing plugins on fresh containers / volume wipes.
            if plugins_cfg.enabled_plugins:
                self._reconcile_missing_plugins(plugins_cfg)

            from pathlib import Path as _Path

            from mewbo_core.agents.agent_registry import AgentRegistry, parse_agent_file
            from mewbo_core.hooks import merge_plugin_hooks
            from mewbo_core.tooling.plugins import load_all_plugin_components

            fan_out = load_all_plugin_components()
            self._agent_registry = AgentRegistry()

            self._skill_registry.load_plugin_components(fan_out)
            for pc in fan_out.components:
                if pc.manifest is None:
                    continue
                plugin_source = f"plugin:{pc.manifest.name}"
                plugin_caps = pc.manifest.requires_capabilities
                plugin_root = pc.manifest.install_path
                for af in pc.agent_files:
                    agent_def = parse_agent_file(_Path(af), source=plugin_source)
                    if agent_def is None:
                        continue
                    self._agent_registry.register(
                        agent_def,
                        capabilities=plugin_caps,
                        plugin_root=plugin_root,
                    )
                # Load this plugin's session tools WITH its capability gate, so a
                # capability-gated session tool (e.g. the ``scg`` suite's
                # ``scg_*``) surfaces to any session that holds the capability —
                # including via the runtime grant — not only when the
                # client lists the tool in ``allowed_tools``. Same
                # data-driven gate the AgentDefs register through above.
                for entry in pc.session_tool_entries:
                    self._session_tool_registry.load_entry(
                        entry, requires_capabilities=plugin_caps
                    )
            for hooks_json, plugin_root in fan_out.hooks_configs:
                merge_plugin_hooks(self._hook_manager, hooks_json, plugin_root)
            plugin_mcp_servers = fan_out.mcp_servers
        else:
            plugin_mcp_servers = {}

        # Reuse a cached registry across runs whose build inputs (cwd +
        # plugin-contributed MCP servers + MCP-config fingerprint) are identical,
        # instead of rebuilding per query — the per-run rebuild was on the
        # critical path to the first FE-visible event. An explicit
        # ``tool_registry`` (tests, structured runners) still bypasses the cache.
        # The cache key already covers ``(cwd, extra_mcp_servers, mcp-config
        # fingerprint)``, so a run attaching its own servers gets its own entry
        # instead of poisoning the shared one — which is what makes merging a
        # per-run set in here safe at all.
        self._tool_registry = tool_registry or get_or_build_registry(
            cwd=cwd,
            extra_mcp_servers={**plugin_mcp_servers, **self._session_mcp_servers} or None,
        )
        self._context_builder = ContextBuilder(self._session_store)

        # Register lossless micro-compaction as a pre_compact hook.
        from mewbo_core.session.compaction import micro_compact_events

        self._hook_manager.pre_compact.append(micro_compact_events)

    def run(
        self,
        user_query: str,
        *,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        return_state: bool = False,
        session_id: str | None = None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        user_turn_persisted: bool = False,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration for a session (sync wrapper around :meth:`arun`).

        Backward-compatible synchronous entry point for CLI / API request
        threads / tests. Owns the event-loop lifecycle via ``asyncio.run``, so
        every existing sync caller behaves exactly as before. Environments that
        already drive an event loop (browser-hosted Pyodide's WebLoop) must call
        :meth:`arun` instead — it awaits the same body with NO nested
        ``asyncio.run`` (the "WebLoop wall").
        """
        return asyncio.run(
            self.arun(
                user_query,
                max_iters=max_iters,
                initial_plan=initial_plan,
                return_state=return_state,
                session_id=session_id,
                mode=mode,
                should_cancel=should_cancel,
                allowed_tools=allowed_tools,
                denied_tools=denied_tools,
                strict_tool_scope=strict_tool_scope,
                capability_mode=capability_mode,
                skill_instructions=skill_instructions,
                message_queue=message_queue,
                interrupt_step=interrupt_step,
                user_id=user_id,
                source_platform=source_platform,
                invocation_id=invocation_id,
                extra_session_tools=extra_session_tools,
                enable_skills=enable_skills,
                project_autoselect=project_autoselect,
                attachments=attachments,
                user_turn_persisted=user_turn_persisted,
            )
        )

    async def arun(
        self,
        user_query: str,
        *,
        max_iters: int = 3,
        initial_plan: Plan | None = None,
        return_state: bool = False,
        session_id: str | None = None,
        mode: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        user_turn_persisted: bool = False,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration asynchronously (async-first entry point).

        Awaits the orchestration coroutine directly, so an environment that
        already owns a running event loop (browser-hosted Pyodide's WebLoop,
        async test harnesses) can drive a query with native ``await`` and NO
        nested ``asyncio.run``. The sync :meth:`run` wrapper is the CPython
        entry point and delegates here; the two share this single body.
        """
        if session_id is None:
            session_id = self._session_store.create_session()

        # Fold the session's durable signals (tags + merged context + the
        # entry-point surface) into filterable Langfuse tags/metadata once; the
        # seam propagates them to every child observation. Apps write their
        # context/tags before invoking the runtime, so they are present here.
        #
        # The merged context carries only the CLIENT-ADVERTISED capabilities, so
        # overlay the AUGMENTED set (advertised ∪ runtime-provider grants) before
        # deriving — otherwise a capability granted at runtime (the ``scg``
        # provider) would be live in the run yet invisible in the trace's
        # ``capabilities`` facet. ``derive`` stays a pure transform of
        # (tags, context, surface); we only enrich the context it reads.
        derive_context = dict(self._session_store.latest_context(session_id))
        effective_caps = self._session_capabilities(session_id, allowed_tools=allowed_tools)
        if effective_caps:
            derive_context["client_capabilities"] = list(effective_caps)
        # An explicit caller-supplied user_id always wins. Otherwise, the real
        # principal — stamped into the context payload by the API's
        # ``_stamp_principal_subject`` whenever auth is enabled — is the
        # honest identity for tracing. Leave it None (never the session id)
        # when no principal exists, so the seam's own anonymous fallback
        # applies instead of silently aliasing user_id to session_id.
        if user_id is None:
            principal_subject = derive_context.get("principal_subject")
            if isinstance(principal_subject, str) and principal_subject:
                user_id = principal_subject
        provenance = TraceProvenance.derive(
            tags=self._session_store.tags_for_session(session_id),
            context=derive_context,
            surface=source_platform,
        )

        with session_log_context(session_id):
            with langfuse_session_context(
                session_id,
                user_id=user_id,
                invocation_id=invocation_id,
                source_platform=source_platform,
                # The trace name identifies the KIND of turn, never this
                # execution of it: a name carrying a turn index, a session id
                # or the query text mints a fresh name per run and every
                # saved filter, evaluator and dashboard stops matching. The
                # surface is a bounded set, so it stays groupable.
                trace_name=f"turn:{source_platform or 'unknown'}",
                tags=list(provenance.tags),
                metadata=provenance.metadata,
            ):
                return await self._run_with_session_context_async(
                    user_query,
                    max_iters=max_iters,
                    initial_plan=initial_plan,
                    return_state=return_state,
                    session_id=session_id,
                    mode=mode,
                    should_cancel=should_cancel,
                    allowed_tools=allowed_tools,
                    denied_tools=denied_tools,
                    strict_tool_scope=strict_tool_scope,
                    capability_mode=capability_mode,
                    skill_instructions=skill_instructions,
                    message_queue=message_queue,
                    interrupt_step=interrupt_step,
                    extra_session_tools=extra_session_tools,
                    enable_skills=enable_skills,
                    project_autoselect=project_autoselect,
                    attachments=attachments,
                    user_turn_persisted=user_turn_persisted,
                    provenance=provenance,
                )

    async def _run_with_session_context_async(
        self,
        user_query: str,
        *,
        max_iters: int,
        initial_plan: Plan | None,
        return_state: bool,
        session_id: str,
        mode: str | None,
        should_cancel: Callable[[], bool] | None,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        project_autoselect: bool = False,
        attachments: list[dict] | None = None,
        user_turn_persisted: bool = False,
        provenance: TraceProvenance | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration with Langfuse session context set.

        Async body shared by the sync :meth:`run` wrapper and the async-first
        :meth:`arun`. Every LLM-driven leg is awaited directly (never wrapped
        in ``asyncio.run``) so it composes on a caller-owned running loop.
        """
        state = OrchestrationState(goal=user_query, session_id=session_id)
        resolved_mode = self._resolve_mode(mode)
        state.summary = self._session_store.load_summary(session_id)
        state.tool_results = state.tool_results or []
        state.open_questions = state.open_questions or []
        task_queue: TaskQueue | None = None

        self._hook_manager.run_on_session_start(session_id)
        error_msg: str | None = None
        try:
            # Record the turn — unless the seam that ACCEPTED it already did.
            # ``SessionRuntime.start_async`` writes the ``user`` event the moment
            # it accepts, because this body only reaches here after
            # ``Orchestrator.__init__``'s heavy synchronous setup and the turn
            # would be invisible to every client for that whole window. Suppress,
            # never deduplicate by content: two identical consecutive queries are
            # legitimate, so comparing text would silently drop a real turn.
            # Default ``False`` keeps every direct caller (CLI turn engine,
            # structured runners) writing it here exactly as before.
            if not user_turn_persisted:
                self._session_store.append_user_turn(session_id, user_query, attachments)
            if self._should_update_summary(user_query):
                state.summary = self._update_summary_with_memory(
                    session_id,
                    user_query.strip(),
                )

            updated_summary = await self._maybe_auto_compact_async(session_id)
            if updated_summary:
                state.summary = updated_summary

            # Server-registry slash commands (``/compact``, ``/skills``,
            # ``/tokens``, ``/fork``, ``/tag``, ``/help``) all dispatch
            # through ``mewbo_core.session.commands.execute_command`` so the
            # operation is single-sourced regardless of which UI typed
            # them — CLI ``run_sync`` route, API channel pipeline, or
            # console palette. Per-command rendering still belongs to
            # the calling UI; the orchestrator only short-circuits the
            # tool-use loop and surfaces the rendered ``result.body``.
            stripped_query = user_query.strip()
            parts = stripped_query.split(maxsplit=1)
            if parts and parts[0].startswith("/"):
                cmd_name = parts[0][1:]
                from mewbo_core.session.commands import (
                    COMMANDS,
                    CommandContext,
                    execute_command,
                )

                if cmd_name in COMMANDS:
                    raw_remainder = parts[1] if len(parts) > 1 else ""
                    # ``/compact`` takes the whole remainder as a single
                    # focus directive; other handlers expect token args.
                    cmd_args = (
                        [raw_remainder]
                        if cmd_name == "compact" and raw_remainder
                        else raw_remainder.split()
                    )
                    cmd_ctx = CommandContext(
                        session_id=session_id,
                        session_store=self._session_store,
                        hook_manager=self._hook_manager,
                        model_name=self._model_name,
                    )
                    try:
                        result = await execute_command(cmd_name, cmd_args, cmd_ctx)
                        if cmd_name == "compact":
                            state.summary = self._session_store.load_summary(session_id) or ""
                            state.done_reason = "compacted"
                        else:
                            state.done_reason = f"command:{cmd_name}"
                        state.done = True
                        task_queue = self._build_direct_response(result.body)
                    except Exception as exc:
                        logging.warning("User-initiated /%s failed", cmd_name, exc_info=True)
                        state.done = True
                        state.done_reason = (
                            "compact_failed"
                            if cmd_name == "compact"
                            else f"command_failed:{cmd_name}"
                        )
                        message = (
                            f"Compaction failed: {exc}. Session continues uncompacted."
                            if cmd_name == "compact"
                            else f"/{cmd_name} failed: {exc}. Session continues."
                        )
                        task_queue = self._build_direct_response(message)
                    return (task_queue, state) if return_state else task_queue

            context = self._context_builder.build(
                session_id=session_id,
                user_query=user_query,
                model_name=self._model_name,
            )
            # Always pass the FULL tool spec set to the loop; plan-mode
            # filtering (read-only + configured edit tool + exit_plan_mode)
            # happens inside ``ToolUseLoop._bind_model`` so tools can be
            # re-bound after plan approval without reconstructing specs.
            tool_specs = self._tool_registry.list_specs()
            # Three-state: ``None`` skips the gate entirely (unrestricted); ``[]``
            # is an explicit empty grant and MUST reach ``filter_specs``, which
            # tests it with ``is None`` for the same reason.
            if allowed_tools is not None:
                allowed_tools = self._admit_attached_mcp_tools(allowed_tools)
                if strict_tool_scope:
                    # Strict mode: ``allowed_tools`` is authoritative —
                    # nothing outside it survives, not even built-ins.
                    # Used by wiki-qa so the agent doesn't waste round
                    # trips trying ``aider_shell_tool`` / ``spawn_agent``
                    # / etc. Caller is responsible for including any core
                    # tool it actually needs in ``allowed_tools``.
                    tool_specs = filter_specs(
                        tool_specs,
                        allowed=allowed_tools,
                        denied=denied_tools,
                        capability_mode=capability_mode,
                    )
                else:
                    # Permissive mode (FE default): ``allowed_tools`` only
                    # scopes MCP tools; built-in tools always stay.
                    builtin_ids = [s.tool_id for s in tool_specs if s.kind != "mcp"]
                    tool_specs = filter_specs(
                        tool_specs,
                        allowed=allowed_tools + builtin_ids,
                        denied=denied_tools,
                        capability_mode=capability_mode,
                    )
            elif capability_mode != "all" or denied_tools:
                # No allowlist, but a ROOT capability ceiling applies (a role
                # narrowed a session to ``read_only``) or the caller named an
                # explicit deny. Apply the coarse privilege gate + deny to the
                # registry specs so a read-only session binds only read-tier
                # tools + ``always_load`` — mirroring the per-tier filter a
                # spawned child gets. Guarded so an unrestricted, undenied
                # session skips ``filter_specs`` entirely, and so
                # ``agent.default_denied_tools`` is never applied to a session
                # that named neither a ceiling nor a deny.
                tool_specs = filter_specs(
                    tool_specs, denied=denied_tools, capability_mode=capability_mode
                )

            # Resolve session capabilities once so every downstream lookup
            # (slash-command skill activation, sub-agent catalog, activate_skill
            # tool dispatch) sees the same client-advertised set. Threading
            # allowed_tools derives any product-tool selection into a
            # request-scoped grant too — see _session_capabilities.
            session_caps = self._session_capabilities(session_id, allowed_tools=allowed_tools)

            # Skill invocation detection and hot-reload.
            self._skill_registry.maybe_reload()
            if skill_instructions is None:
                _si, _ts = self._try_skill_invocation(user_query, tool_specs, session_caps)
                if _si is not None:
                    skill_instructions = _si
                if _ts is not None:
                    tool_specs = _ts

            # Unified path: always enter the tool-use loop. Plan mode is
            # enforced inside the loop via tool filtering + path-scoped
            # permission checks + the ``exit_plan_mode`` approval gate.
            if resolved_mode == "plan":
                # Ensure the session's scoped plan directory exists before
                # the model starts so the edit tool can write to plan.md.
                ensure_plan_dir(session_id)
                state.plan_path = plan_file_for(session_id)

            max_depth = int(get_config_value("agent", "max_depth", default=5))
            max_concurrent = int(get_config_value("agent", "max_concurrent", default=20))
            attestation = None
            if bool(get_config_value("agent", "attestation_enabled", default=True)):
                # Seed from the store's last persisted
                # record so a recovered/continued session's chain links
                # continuously instead of restarting at genesis.
                from mewbo_core.agents.attestation import AttestationChain

                seed = self._session_store.last_attestation_hash(session_id)
                attestation = AttestationChain(session_id=session_id, head=seed)
            registry = AgentHypervisor(
                max_concurrent=max_concurrent,
                session_step_budget=self._session_step_budget,
                attestation=attestation,
            )
            root_ctx = AgentContext.root(
                model_name=self._model_name,
                max_depth=max_depth,
                fallback_models=self._fallback_models,
                should_cancel=should_cancel,
                event_logger=lambda event: self._session_store.append_event(session_id, event),
                registry=registry,
                message_queue=message_queue,
                interrupt_step=interrupt_step,
                # Seed the filesystem-containment axis from config;
                # narrowed per sub-agent thereafter. Defaults to "full_access"
                # because the ROOT agent acts directly for the operator —
                # containment attenuates privilege across a spawn, and it is the
                # child (default "workspace_write") that the tier confines.
                workspace_mode=str(
                    get_config_value("agent", "default_workspace_mode", default="full_access")
                ),
                # Seed the delegation-privilege axis from the caller's resolved
                # ceiling. "all" (the default) is a no-op — every child then
                # narrows from here. A role-narrowed session ("read_only" for a
                # viewer) caps the ROOT's own session tools too, since the loop's
                # ``build_for`` reads ``agent_context.capability_mode``.
                capability_mode=capability_mode,
            )
            loop = ToolUseLoop(
                agent_context=root_ctx,
                tool_registry=self._tool_registry,
                permission_policy=self._permission_policy,
                approval_callback=self._approval_callback,
                hook_manager=self._hook_manager,
                safety_plane=self._safety_plane,
                project_instructions=self._project_instructions,
                user_instructions=self._resolve_user_instructions(
                    session_id=session_id,
                    session_caps=session_caps,
                    tool_specs=tool_specs,
                    # The same inputs the loop below is handed, so the tools
                    # named in the operator's template match the ones it builds
                    # — INCLUDING strict_tool_scope, or a permissive FE root's
                    # {{ tools }} catalog drops schedule_trigger the agent holds.
                    allowed_tools=allowed_tools,
                    denied_tools=denied_tools,
                    extra_session_tools=extra_session_tools,
                    provenance=provenance,
                    strict_tool_scope=strict_tool_scope,
                    capability_mode=capability_mode,
                ),
                skill_instructions=skill_instructions,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                session_tool_registry=self._session_tool_registry,
                # Forward the caller's allowlist so plugin session tools
                # (e.g. wiki_clone_repo for the wiki indexer) get built for
                # root agents that need them. ``None`` still means "no
                # plugin session tools" for plain user sessions.
                allowed_tools=allowed_tools,
                # Deny wins over everything else the loop's session-tool gates
                # admit — the unconditional auto-surface and the capability
                # auto-surface included. Same list this run's registry specs
                # were just filtered by, so a denied tool id is off BOTH
                # surfaces, not just one of them.
                denied_tools=denied_tools,
                # Whether ``allowed_tools`` is authoritative (strict) or a
                # permissive MCP ceiling — mirrors the ``filter_specs`` branch
                # above so the loop's spawn_agent gate reads the same intent.
                strict_tool_scope=strict_tool_scope,
                cwd=self._cwd,
                session_id=session_id,
                session_capabilities=session_caps,
                extra_session_tools=extra_session_tools,
                enable_skills=enable_skills,
                project_autoselect=project_autoselect,
                # Assembled only for a run that opted in — see
                # ``_build_project_catalog``.
                project_catalog=(
                    self._build_project_catalog() if project_autoselect else None
                ),
                # The loop holds no transcript access, so a project switch
                # cannot read back what it must carry forward without this.
                session_context_reader=lambda: self._last_context(session_id),
            )
            try:
                task_queue, state = await loop.run(
                    user_query,
                    tool_specs=tool_specs,
                    context=context,
                    plan=initial_plan,
                    mode=resolved_mode,
                )
            finally:
                # In the ``finally`` so a run that RAISED still reports the model
                # that was live when it died — that is the case the failure
                # record exists for.
                self._served_model = loop.active_model
                # Snapshot the ownership index BEFORE cleanup force-settles it.
                # Once cleanup has run every handle reports terminal, and the
                # evidence that this run declared itself finished while work it
                # owned was still live is gone with it — so the read has to
                # happen here, not after.
                unsettled_children = await self._unsettled_children(registry)
                # Belt-and-suspenders: ensure all agents cleaned up.
                try:
                    await registry.cleanup(timeout=5.0)
                except Exception:
                    pass
            state.session_id = session_id
            if resolved_mode == "plan":
                state.plan_path = plan_file_for(session_id)

            # Emit assistant response event. Every user turn must have
            # exactly one materialised assistant event in the transcript
            # — if ``task_result`` is empty (e.g. ``max_steps_reached``
            # with no synthesis), write a synthetic closure marker so the
            # UI timeline finalises the turn and the LLM's
            # ``recent_events`` gains narrative closure.
            if task_queue.task_result:
                self._session_store.append_event(
                    session_id,
                    {"type": "assistant", "payload": {"text": task_queue.task_result}},
                )
            else:
                closure = _format_assistant_closure(state.done_reason, task_queue.last_error)
                self._session_store.append_event(
                    session_id,
                    {"type": "assistant", "payload": {"text": closure}},
                )

            self._maybe_generate_title(session_id)

            if not state.done:  # pragma: no cover - defensive guard
                state.done = True
                state.done_reason = "max_iterations_reached"

            # Promise-as-completion gate. A clean terminal is a CLAIM that the
            # work is finished, and the claim is false while runs this session
            # owns are still live. Children are collected at loop teardown, so a
            # handle still non-terminal here was abandoned mid-flight: the run
            # ended, the work did not. Downgrading the reason is what stops that
            # from presenting as success — ``completed`` is the one status that
            # withholds the recovery affordance, and a session with abandoned
            # work is exactly one that needs it.
            if state.done_reason == "completed" and unsettled_children:
                state.done_reason = "unmet_goal"
                if not task_queue.last_error:
                    task_queue.last_error = (
                        f"Run ended while {len(unsettled_children)} owned sub-agent "
                        "run(s) were still live; their work was abandoned rather "
                        "than collected."
                    )

            # Annotated as the TypedDict rather than ``dict[str, object]`` so
            # the checker validates every key against ``CompletionPayload``;
            # a bare literal widens to ``dict[str, str | dict[...]]`` the moment
            # a nested value is added and stops matching the event contract.
            completion_payload: CompletionPayload = {
                "done": state.done,
                "done_reason": state.done_reason,
                "task_result": task_queue.task_result,
            }
            # The loop leaves ``done_reason`` at "completed" for a run that died
            # against a credential, a network path or a quota, so this code is
            # the ONLY thing that carries that fact out of the run. Omitted
            # when absent, so a run that hit no wall carries no key.
            if state.blocked_code:
                completion_payload["blocked_code"] = state.blocked_code
            self._attach_failure_record(completion_payload, task_queue, state)
            self._session_store.append_event(
                session_id,
                {"type": "completion", "payload": completion_payload},
            )

            note = self._promise_note(
                task_queue.task_result or "",
                owned_runs_live=bool(unsettled_children),
            )
            if note is not None:
                self._session_store.append_event(
                    session_id,
                    {"type": "run_note", "payload": {"text": note}},
                )

            updated_summary = await self._maybe_auto_compact_async(session_id)
            if updated_summary:
                state.summary = updated_summary

            return (task_queue, state) if return_state else task_queue
        except Exception as exc:
            logging.exception("Orchestration failed for session {}", session_id)
            if task_queue is None:
                task_queue = TaskQueue(_human_message=user_query, action_steps=[])
            # Classify + clamp ONCE, and let that ONE bounded projection feed
            # every consumer. A raw ``str(exc)`` here is how an upstream HTML
            # error page (thousands of characters, provider-controlled) reached
            # the transcript, the completion payload, the next run's
            # ``recent_events`` bullet list — and, via ``error_msg`` below, the
            # ``on_session_end`` hook, whose string a channel adapter posts
            # verbatim into a forge PR comment or a chat message. That last one
            # is OUTWARD-facing, so it must never carry provider markup.
            run_error = RunError.from_exception(exc, model=self._error_model)
            error_msg = run_error.brief()
            task_queue.last_error = error_msg
            state.done = True
            state.done_reason = "error"
            # Closure marker so the failed turn is always materialised in
            # the UI timeline and the LLM's ``recent_events`` carries
            # narrative closure into the next recovery run. The one-line
            # ``title`` keeps that marker readable — the full diagnostic
            # lives on the completion event's ``error_detail``.
            self._session_store.append_event(
                session_id,
                {
                    "type": "assistant",
                    "payload": {
                        "text": _format_assistant_closure(state.done_reason, run_error.title)
                    },
                },
            )
            failure_payload: CompletionPayload = {
                "done": True,
                "done_reason": state.done_reason,
                "task_result": task_queue.task_result,
                "error": error_msg,
                "last_error": error_msg,
                "error_detail": run_error.model_dump(mode="json"),
            }
            # A run that RAISED can still have hit a wall first — a repo it
            # could not reach, a quota it spent — and that wall is the more
            # actionable half of the story. Carried on this path too, or a
            # blocked run that then died of something else reads as a generic
            # failure with nothing for the user to fix.
            if state.blocked_code:
                failure_payload["blocked_code"] = state.blocked_code
            self._session_store.append_event(
                session_id,
                {"type": "completion", "payload": failure_payload},
            )
            return (task_queue, state) if return_state else task_queue
        finally:
            self._record_outcome_assertions(session_id, error_msg)

    def _record_outcome_assertions(self, session_id: str, error_msg: str | None) -> None:
        """Run the session-end hooks and persist anything they assert.

        A session-end hook is the only component that can see whether the
        session's PURPOSE succeeded — it holds the owning job, while the loop
        holds only its own signals, every one of which can say success while the
        job never reached its terminal state. The assertion lands as its own
        transcript event AFTER the completion, which is where the status
        derivation can consume it without this seam having to reorder the hook
        dispatch ahead of the terminal it describes.

        TOTAL BY CONSTRUCTION, because this runs inside ``run``'s ``finally``: a
        raise here would REPLACE whatever exception the run was already
        propagating, reporting a genuine orchestration failure as a hook bug.
        That also covers a ``HookManager`` predating the return channel — it
        reports ``None``, which is simply no assertion, never an error.
        """
        try:
            assertions = self._hook_manager.run_on_session_end(session_id, error_msg)
        except Exception:  # pragma: no cover - defensive; a finally must not raise
            logging.warning(
                "session-end hooks failed for session {}", session_id, exc_info=True
            )
            return
        if not isinstance(assertions, list):
            return
        for assertion in assertions:
            try:
                self._session_store.append_event(
                    session_id,
                    {
                        "type": "outcome_assertion",
                        "payload": assertion.model_dump(mode="json"),
                    },
                )
            except Exception:  # pragma: no cover - defensive; see above
                logging.warning(
                    "could not persist an outcome assertion for session {}",
                    session_id,
                    exc_info=True,
                )

    def _attach_failure_record(
        self,
        payload: CompletionPayload,
        task_queue: TaskQueue,
        state: OrchestrationState,
    ) -> None:
        """Attach the bounded failure record to a terminal completion payload.

        ``task_queue.last_error`` is a STICKY diagnostic — a mid-run tool
        failure the run continued past still leaves it set. It is CLAMPED
        whenever set, whatever the outcome: its readers do not check
        ``done_reason`` (the scg map-job persists it onto the job record, the
        CLI prints it on /retry|/continue|/edit), so gating the clamp let a run
        that recovered and finished clean carry a raw multi-KB provider page out
        to them.

        ``error`` is WITHHELD on a run whose terminal status is ``completed``,
        because that ONE key is what a client renders as a user-facing error
        card. A tool call that failed and was recovered from is not a session
        failure, and emitting it as one puts an error card under a complete,
        correct answer — which is exactly what it did. This is the same rule the
        no-sticky-string branch below already applied; it is stated once here
        and applied to both.

        The RECORD is still carried on such a run: ``last_error`` and
        ``error_detail`` both ride the payload, and no client renders a card on
        either. That is what keeps the auditability guarantee intact — the runs
        a blanket withhold would silence are overwhelmingly the LAUNDERED ones
        (a halt presenting as success), and dropping the one field able to
        contradict the status is what turns a wrong status into an unfalsifiable
        one. A status is only worth trusting if the record can be used to check
        it, so the record stays and only the render trigger goes.

        The gate reads ``OrchestrationState.terminal_status`` rather than
        comparing ``done_reason`` here: that projection also folds in
        ``verified is False`` and the cancelled/unachieved vocabularies, and a
        second copy of it at this call site could only ever drift from it. Note
        ``done_reason == "error"`` never reaches here — the path that mints it
        raises, and its handler builds its own failure payload.

        ``blocked_code`` is consulted INDEPENDENTLY of that projection, exactly
        as the status layer and the console already consult it. A run that died
        against a credential, a network path or a quota deliberately keeps
        ``done_reason == "completed"`` and carries the wall only in that field,
        so ``terminal_status`` calls it a success — and gating on the projection
        alone withheld the error from precisely the runs a user most needs to
        see, silently, since a client that reads neither field then renders a
        clean success.

        A run that stopped short leaving NO sticky string (a doom-loop halt, a
        spent budget, a failed ground-truth check) still gets a structured
        record here — but only the additive ``error_detail``, never ``error``,
        for the same reason: a halt that produced a wrap-up answer is not an
        error to put in front of a user.
        """
        if task_queue.last_error:
            run_error = RunError.from_message(task_queue.last_error, model=self._error_model)
            brief = run_error.brief()
            task_queue.last_error = brief
            payload["last_error"] = brief
            payload["error_detail"] = run_error.model_dump(mode="json")
            if state.terminal_status() != "completed" or state.blocked_code is not None:
                payload["error"] = brief
        elif state.done_reason in UNACHIEVED_DONE_REASONS:
            payload["error_detail"] = RunError.from_message(
                f"Run ended without reaching its goal ({state.done_reason}).",
                model=self._error_model,
            ).model_dump(mode="json")

    @property
    def _error_model(self) -> str:
        """The model a failure record should name — served if known, else configured.

        ``RunError._build`` falls back to whatever model it is handed whenever
        the message text carries no provider token, so this ONE reader is what
        decides whether a failure blames the model that ran or the one that was
        configured four minutes earlier.
        """
        return self._served_model or self._model_name

    @staticmethod
    async def _unsettled_children(registry: AgentHypervisor) -> list[str]:
        """Ids of agents still non-terminal as the run tears down.

        The ownership index the promise-as-completion gate reads. Must be called
        BEFORE ``registry.cleanup``, which force-settles every handle and erases
        the distinction between "this run finished its work" and "this run
        merely stopped".

        Read-only and total: a registry that cannot answer reports nothing owed
        rather than failing a run that has otherwise succeeded.
        """
        try:
            agents = await registry.list_all()
        except Exception:  # pragma: no cover - defensive; never fail a run here
            return []
        return [h.agent_id for h in agents if h.status in ACTIVE_STATUSES]

    @staticmethod
    def _promise_note(text: str, *, owned_runs_live: bool) -> str | None:
        """Return ONE factual reminder when terminal prose promises future work.

        The prose half of promise-as-completion: a terminal that reads "I'll
        check back on it shortly" and then ends zero seconds later is not
        masking an error, it is fabricating a future. Nothing downstream can
        falsify that from the record, because there is no error to find.

        What this returns is a statement of fact the record already proves — no
        background work is scheduled — and never a verdict on the output. It is
        deliberately silent when owned runs ARE live, because then the promise
        is simply true, and silent on non-matching prose, because a heuristic
        that guesses at intent would invent the very unfalsifiable claim this
        seam exists to remove.
        """
        if owned_runs_live or not text:
            return None
        probe = text.lower()
        if not any(marker in probe for marker in _FUTURE_COMMITMENT_MARKERS):
            return None
        return (
            "This turn ended with no scheduled or running background work. "
            "Any follow-up stated in the response above will not happen on "
            "its own."
        )

    # ------------------------------------------------------------------
    # Session helpers (kept from original)
    # ------------------------------------------------------------------

    def _admit_attached_mcp_tools(self, allowed_tools: list[str]) -> list[str]:
        """Union this run's attached-MCP tool ids into *allowed_tools*.

        A server this run explicitly attached is admitted by the ACT of attaching
        it. Without this the servers are discovered and then filtered straight
        back out — and ``filter_specs`` drops an unrecognised id in SILENCE, so
        the caller would see no error, no tools, and nothing to grep for.

        A caller cannot name the ids itself: they are not knowable until
        discovery has run, which happens in this object's constructor. That is
        the whole reason the grant is expressed as "attach the server" rather
        than as an allowlist entry.

        Returns *allowed_tools* unchanged (same list object) when nothing was
        attached, which is nearly every run.
        """
        attached = self._session_mcp_tool_ids()
        return list(allowed_tools) + attached if attached else allowed_tools

    def _session_mcp_tool_ids(self) -> list[str]:
        """Registry tool ids contributed by this run's own attached MCP servers.

        Empty when nothing was attached, so a run that attaches nothing leaves
        ``allowed_tools`` untouched and never walks the spec list.

        A server that discovered NO tools contributes nothing and is reported —
        it means the server was unreachable or exposes nothing, and the only
        other symptom would be its tools quietly never being callable.
        """
        if not self._session_mcp_servers:
            return []
        ids: list[str] = []
        seen_servers: set[str] = set()
        for spec in self._tool_registry.list_specs():
            server = str(spec.metadata.get("server", ""))
            if server in self._session_mcp_servers:
                ids.append(spec.tool_id)
                seen_servers.add(server)
        silent = sorted(set(self._session_mcp_servers) - seen_servers)
        if silent:
            logging.warning(
                "attached MCP servers contributed no tools: {} "
                "(unreachable, or they expose none)",
                silent,
            )
        return ids

    def _session_capabilities(
        self, session_id: str, *, allowed_tools: list[str] | None = None
    ) -> tuple[str, ...]:
        """Return the capability tuple in effect for *session_id*.

        Reads ``client_capabilities`` from the most recently appended
        ``context`` event (set by the API from the ``X-Mewbo-Capabilities``
        header). Unions in capabilities DERIVED from *allowed_tools*
        — REQUEST-SCOPED, never persisted: when the caller names
        a product ``SessionTool``'s id (e.g. ``wiki_search_pages``) in this
        request's allowlist, ``SessionToolRegistry.capabilities_for`` looks up
        the tool's plugin-manifest ``requires_capabilities`` and unions it in
        for THIS call only. This closes the asymmetry: selecting a
        product tool via the SAME ``context.mcp_tools`` field that already
        gates ``SessionToolRegistry.build_for``'s allowlist now *also* unlocks
        its AgentDef family on the capability-only catalog gate
        (``filter_by_capabilities``). Because the derivation is fresh per
        request rather than written to a context event, omitting the tool on
        a later turn naturally revokes it — unlike the sticky, additive-only
        header path, which this leaves untouched (the two unions
        harmlessly on top of each other). Finally unions in any RUNTIME
        grants registered by a capability library above core
        (``augment_session_capabilities``) — so a capability gated on a live
        predicate (e.g. ``scg`` once the SCG is enabled AND a source is
        mapped) surfaces to an ORDINARY session without the client
        advertising it. Returns an empty tuple on any error or when nothing
        applies.
        """
        from mewbo_core.capabilities import (
            augment_session_capabilities,
            parse_capabilities,
        )

        try:
            events = self._session_store.load_transcript(session_id)
        except Exception:
            events = []
        advertised: object = None
        for event in events:
            if event.get("type") != "context":
                continue
            payload = event.get("payload")
            if isinstance(payload, dict) and "client_capabilities" in payload:
                advertised = payload["client_capabilities"]
        derived = self._session_tool_registry.capabilities_for(allowed_tools or [])
        base = set(parse_capabilities(advertised)) | set(derived)
        return augment_session_capabilities(tuple(sorted(base)))

    def _system_instructions_store(self) -> SystemInstructionsStoreBase | None:
        """Return the custom-instructions store, resolving the factory once.

        Resolution is lazy + sticky: the factory raises when the configured
        driver is ``mongodb`` and Mongo is unreachable, and this feature must
        never be the reason a session refuses to start. A failure is logged once
        and remembered, so a dead connection isn't re-probed every run.
        """
        if self._instructions_store_tried:
            return self._instructions_store
        self._instructions_store_tried = True
        try:
            self._instructions_store = create_system_instructions_store()
        except Exception:
            logging.warning(
                "Custom system instructions unavailable (store unreachable); "
                "running without them.",
                exc_info=True,
            )
            self._instructions_store = None
        return self._instructions_store

    def _resolve_instruction_tools(
        self,
        *,
        tool_specs: list[ToolSpec],
        allowed_tools: list[str] | None,
        session_caps: tuple[str, ...],
        extra_session_tools: list[SessionTool] | None,
        strict_tool_scope: bool,
        capability_mode: str = "all",
        denied_tools: list[str] | None = None,
    ) -> tuple[str, ...]:
        """The tool ids an operator's template sees in ``InstructionContext.tools``.

        The ``ToolRegistry`` specs bound for this run, UNIONED with the session
        tools the root agent will actually be built with. The union is
        load-bearing: the registry specs alone omit every session tool
        (``wiki_*``, ``scg_*``, ``submit_widget``, ``schedule_trigger``), so
        ``'wiki_search' in tools`` silently renders False for an agent that
        genuinely holds it and a template branching on a product tool can never
        fire.

        The session-tool half is resolved through ``SessionToolRegistry.ids_for``,
        the SAME selection ``ToolUseLoop`` builds from, so this list cannot drift
        from what the agent gets. Caller-injected tools (``extra_session_tools``,
        e.g. the structured-response emit tool) are unioned in too — they are
        bound for this run just as truly.

        DELIBERATELY OMITTED: the five internals the loop injects for ITSELF
        (``spawn_agent``/``spawn_agents``, ``update_todos``, ``exit_plan_mode``,
        ``activate_skill``). They are decided INSIDE ``ToolUseLoop``, downstream
        of this call, and several are mode-dependent (``update_todos`` is
        act-mode, ``exit_plan_mode`` is plan-mode), so naming them here would
        trade one lie for another. The field's ``description`` states the
        omission outright — honesty over completeness.

        ``tool_search`` is NOT one of them and IS included: despite being loop
        machinery, it is a genuine ``ToolRegistry`` spec (``always_load``), so it
        arrives through *tool_specs* like any other tool. Listing it as omitted
        would itself have been a lie — the exact failure this method exists to
        fix, one level down.
        """
        ids = {spec.tool_id for spec in tool_specs}
        ids.update(
            self._session_tool_registry.ids_for(
                allowed_tools,
                session_capabilities=session_caps,
                # Same intent the loop's build_for gets, or the operator's
                # {{ tools }} catalog drifts from what the agent holds:
                # a permissive FE root genuinely holds schedule_trigger, and a
                # role-narrowed root drops the write-tier session tools its
                # ``capability_mode`` withholds. ``denied_tools`` for the same
                # reason — the drift law this method exists to enforce.
                denied_tools=denied_tools,
                strict_tool_scope=strict_tool_scope,
                capability_mode=capability_mode,
            )
        )
        ids.update(tool.tool_id for tool in extra_session_tools or [])
        return tuple(sorted(ids))

    def _resolve_user_instructions(
        self,
        *,
        session_id: str,
        session_caps: tuple[str, ...],
        tool_specs: list[ToolSpec],
        allowed_tools: list[str] | None,
        extra_session_tools: list[SessionTool] | None,
        provenance: TraceProvenance | None,
        strict_tool_scope: bool,
        capability_mode: str = "all",
        denied_tools: list[str] | None = None,
    ) -> str | None:
        """Render the operator's custom system instructions for this run.

        The ONE resolution point: read the stored template, render it once
        against this run's :class:`InstructionContext`, and hand the plain string
        to the loop (which then survives compaction and the escalation
        re-render because it lives on the loop instance).

        Fully fail-soft by construction — a missing/disabled doc, an unreachable
        store, or a broken template all yield ``None``, and the run proceeds
        exactly as it would have without the feature. Never raises into a run.
        """
        store = self._system_instructions_store()
        if store is None:
            return None
        try:
            doc = store.get()
        except Exception:
            logging.warning("Could not read the custom system instructions.", exc_info=True)
            return None
        if doc is None or not doc.enabled or not doc.template.strip():
            return None

        surface = provenance.surface if provenance else "unknown"
        context = InstructionContext(
            surface=surface,
            # The enum MEMBER, not its ``.value``. Pydantic coerces the bare
            # string back to the member, so this is runtime-identical — but the
            # field is declared ``SessionOrigin`` and ``describe()`` walks the
            # schema to generate the operator-facing variable reference, so the
            # typed contract has to be honoured at the boundary rather than
            # widened to satisfy a caller.
            origin=provenance.origin if provenance else SessionOrigin.USER,
            is_mobile=is_mobile_surface(surface),
            session_id=session_id,
            model=self._model_name,
            cwd=self._cwd or str(Path.cwd()),
            platform=_platform.system().lower(),
            hostname=socket.gethostname(),
            mewbo_version=get_version(),
            capabilities=tuple(session_caps),
            tools=self._resolve_instruction_tools(
                tool_specs=tool_specs,
                allowed_tools=allowed_tools,
                session_caps=session_caps,
                extra_session_tools=extra_session_tools,
                strict_tool_scope=strict_tool_scope,
                capability_mode=capability_mode,
                denied_tools=denied_tools,
            ),
            # ``project`` is absent for a managed worktree too, not just for an
            # unscoped session: ``TraceProvenance._facets_from_context`` routes a
            # ``managed:<uuid>`` context value to a ``worktree`` facet and never
            # into ``metadata["project"]``. The field's description says so.
            project=provenance.metadata.get("project") if provenance else None,
        )
        rendered = doc.render(context)
        # Persist the render outcome so the settings UI can surface a broken
        # template (which is otherwise invisible: it degrades to no injection
        # rather than to a failure). Best-effort — ``record_error`` swallows.
        store.record_error(doc.id, rendered.error)
        return rendered.text or None

    def _maybe_generate_title(self, session_id: str) -> None:
        """Kick off non-blocking title generation.

        Runs only once per session (guarded by ``load_title`` absence). The
        caller returns immediately; the title appears via a ``title_update``
        event whenever the LLM call finishes. Failures are logged, never
        raised — the first-user-message fallback remains as safety net.

        Two backgrounding strategies keep the sync and async worlds happy:

        * Pyodide / single-threaded runtimes (``sys.platform == "emscripten"``):
          schedule the coroutine on the already-running event loop. The WebLoop
          persists past the per-request lifecycle, so the task survives — and we
          never touch ``threading`` (which is inlined there) nor
          ``asyncio.run`` (illegal inside a running loop).
        * CPython: spawn a daemon thread that owns its own ``asyncio.run``
          lifecycle — identical to the pre-async-surface behaviour.
        """
        if self._session_store.load_title(session_id) is not None:
            return
        if sys.platform == "emscripten":
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._run_title_generation_async(session_id))
                # Hold a strong reference so asyncio can't GC this
                # fire-and-forget task mid-run; the done-callback discards it
                # once finished (see ``self._background_tasks`` in __init__).
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
                return
            except RuntimeError:
                pass  # No running loop yet — fall through to the thread path.
        threading.Thread(
            target=self._run_title_generation,
            args=(session_id,),
            name=f"title-gen-{session_id[:8]}",
            daemon=True,
        ).start()

    def _run_title_generation(self, session_id: str) -> None:
        """Sync worker body for background title generation (CPython thread).

        No try/except here: the coroutine body already catches and logs
        every exception internally (see :meth:`_run_title_generation_async`),
        so ``asyncio.run`` never raises out of this call.
        """
        asyncio.run(self._run_title_generation_async(session_id))

    async def _run_title_generation_async(self, session_id: str) -> None:
        """Async title generation — single source of truth for the LLM call."""
        try:
            from mewbo_core.session.title_generator import generate_session_title

            events = self._session_store.load_transcript(session_id)
            title = await generate_session_title(events)
            if not title:
                return
            self._session_store.save_title(session_id, title)
            self._session_store.append_event(
                session_id,
                {"type": "title_update", "payload": {"title": title}},
            )
        except Exception as exc:
            logging.warning("Title generation failed: {}: {}", type(exc).__name__, exc)

    def _maybe_auto_compact(self, session_id: str) -> str | None:
        """Sync wrapper around :meth:`_maybe_auto_compact_async`.

        Kept for callers that drive compaction from a synchronous context
        (test harnesses asserting the thrash-guard short-circuit).
        """
        return asyncio.run(self._maybe_auto_compact_async(session_id))

    async def _maybe_auto_compact_async(self, session_id: str) -> str | None:
        from mewbo_core.session.compact import (
            CompactionMode,
            compact_conversation,
            record_compaction,
        )
        from mewbo_core.session.token_budget import read_last_input_tokens

        raw_events = self._session_store.load_transcript(session_id)
        # Thrash guard: if the most recent event is a compaction marker, the
        # transcript was already summarized this turn (manual /compact or a
        # prior auto cycle). Re-running on stale ``last_input_tokens`` from
        # before the boundary would clobber the fresh summary with a partial
        # one. Skip — the next real LLM call will refresh the budget read.
        if raw_events and raw_events[-1].get("type") == "context_compacted":
            return None

        events = self._hook_manager.run_pre_compact(raw_events)
        summary = self._session_store.load_summary(session_id)
        last_input_tokens = read_last_input_tokens(events)
        budget = get_token_budget(
            events,
            summary,
            self._model_name,
            last_input_tokens=last_input_tokens,
        )
        if not budget.needs_compact:
            return None
        compact_model = self._model_name
        try:
            result = await compact_conversation(events, CompactionMode.PARTIAL)
            compact_model = result.model or self._model_name
            summary = result.summary
            tokens_saved = result.tokens_saved
        except Exception:
            # Structured compaction failed. Do NOT substitute concatenated raw
            # event text — it would poison the context. Skip this cycle; the
            # next turn will try again.
            logging.warning("Structured compaction failed; skipping cycle", exc_info=True)
            return None
        record_compaction(
            self._session_store,
            self._hook_manager,
            session_id,
            summary=summary,
            mode="auto",
            model=compact_model or "",
            tokens_before=budget.total_tokens,
            tokens_saved=tokens_saved,
            events_summarized=len(events),
        )
        return summary

    @staticmethod
    def _reconcile_missing_plugins(plugins_cfg: PluginsConfig) -> None:
        """Ensure all ``enabled_plugins`` exist in the registry.

        On a fresh container or after a volume wipe, enabled plugins may be
        listed in the config but absent from the registry/cache.  This method
        discovers which are missing and attempts to install them from the
        configured marketplaces.  Errors are logged and skipped — session
        startup should not fail because a plugin couldn't be fetched.
        """
        from mewbo_core.tooling.plugins import (
            discover_installed_plugins,
            discover_marketplace_plugins,
            install_plugin,
        )

        cfg = plugins_cfg
        registry_paths = cfg.resolve_registry_paths()
        installed = discover_installed_plugins(registry_paths=registry_paths)
        installed_names = {pc.manifest.name for pc in installed if pc.manifest is not None}
        missing = [
            name.split("@")[0]
            for name in cfg.enabled_plugins
            if name.split("@")[0] not in installed_names
        ]
        if not missing:
            return

        from mewbo_core.common import get_logger

        _log = get_logger(name="core.orchestrator")
        _log.info("Reconciling {} missing plugin(s): {}", len(missing), missing)

        marketplace_dirs = cfg.resolve_marketplace_dirs()
        available = discover_marketplace_plugins(marketplace_dirs=marketplace_dirs)
        available_by_name = {p["name"]: p for p in available}

        for name in missing:
            match = available_by_name.get(name)
            if match is None:
                _log.warning("Plugin '{}' not found in any marketplace — skipping", name)
                continue
            try:
                install_plugin(
                    name,
                    match["marketplace"],
                    marketplace_dirs=marketplace_dirs,
                    install_base=cfg.resolve_install_dir(),
                )
                _log.info("Auto-installed plugin '{}'", name)
            except Exception as exc:
                _log.warning("Failed to auto-install plugin '{}': {}", name, exc)

    @staticmethod
    def _should_update_summary(text: str) -> bool:
        lowered = text.lower()
        keywords = [
            "remember",
            "note this",
            "save this",
            "pin this",
            "keep this",
            "magic number",
            "magic numbers",
        ]
        return any(keyword in lowered for keyword in keywords)

    def _update_summary_with_memory(self, session_id: str, text: str) -> str:
        summary = self._session_store.load_summary(session_id) or ""
        new_line = f"Memory: {text}"
        lines = [line for line in summary.splitlines() if line.strip()] if summary else []
        if new_line not in lines:
            lines.append(new_line)
        updated = "\n".join(lines[-10:]).strip()
        self._session_store.save_summary(session_id, updated)
        return updated

    @staticmethod
    def _build_direct_response(message: str) -> TaskQueue:
        task_queue = TaskQueue(action_steps=[])
        task_queue.task_result = message
        return task_queue

    def _try_skill_invocation(
        self,
        user_query: str,
        tool_specs: list,
        session_capabilities: tuple[str, ...] = (),
    ) -> tuple[str | None, list | None]:
        """Detect ``/skill-name args`` in the query and activate the skill.

        Honours capability gating so a slash command for a gated skill is
        inert in sessions that haven't advertised the matching capability —
        same semantics as the LLM's ``activate_skill`` tool dispatch.

        Returns ``(skill_instructions, scoped_tool_specs)`` on match,
        or ``(None, None)`` if the query is not a skill invocation.
        """
        query = user_query.strip()
        if not query.startswith("/"):
            return None, None

        parts = query.split(None, 1)
        name = parts[0].lstrip("/")
        args = parts[1] if len(parts) > 1 else ""

        skill = self._skill_registry.get(name, session_capabilities)
        if skill is None:
            return None, None

        logging.info("Activating skill '{}' with args '{}'", name, args)
        instructions, scoped_specs = activate_skill(skill, args, tool_specs, cwd=self._cwd)
        return instructions, scoped_specs

    def _last_context(self, session_id: str) -> dict[str, object]:
        """The session's effective context: the most-recent payload, VERBATIM.

        Deliberately NOT ``SessionStoreBase.latest_context``, which FOLDS every
        context event. The two are different questions and this is the one the
        consumers of a re-written context event ask: the API's
        ``_load_last_context`` and the console's ``getLastContext`` both reverse-
        scan for the newest payload and use it as-is. A writer that wants to be
        neutral for them has to carry forward what THEY would have read, and a
        fold would hand back fields an earlier turn deliberately cleared.

        The scan is ``latest_event_of_type`` on the store, beside
        ``merge_context_events`` — the two reducers are a pair and both live
        there, so the API's copy and this one ask one implementation rather than
        each carrying its own. ``O(1)`` on the Mongo driver,
        ``O(one session)`` on the base; bounded by the TYPE, never by a count,
        because the newest context event sits arbitrarily far back after a long
        run and a window that missed it would hand the writer an empty payload to
        carry forward — silently clearing the very fields it exists to preserve.

        Returns a COPY: the caller merges into it before persisting the result as
        the next context event.
        """
        event = self._session_store.latest_event_of_type(session_id, "context")
        payload = event.get("payload") if event else None
        return dict(payload) if isinstance(payload, dict) else {}

    @staticmethod
    def _build_project_catalog() -> ProjectCatalog:
        """Assemble the catalog the project-selection tools read and resolve through.

        Built here rather than in ``__init__`` because it is only ever needed by
        a run that opted into project autoselect: constructing it opens the
        project and repository stores, and an ordinary run has no reason to
        touch either. Each store is optional — a backend that refuses to open
        costs that ONE section of the catalog, exactly as a backend that raises
        while LISTING does, rather than leaving the model with no projects at
        all.

        ``checkout_locator`` is deliberately left unset. Matching a repository
        identity against a managed project's git remotes is the API app's job,
        above core in the DAG, so a core-only caller gets registered
        repositories listed WITHOUT a path — the honest answer here rather than
        a guessed one.
        """
        from mewbo_core.workspaces.project_catalog import ProjectCatalog
        from mewbo_core.workspaces.project_store import ProjectStoreBase, create_project_store
        from mewbo_core.workspaces.repository_store import (
            RepositoryStoreBase,
            create_repository_store,
        )

        project_store: ProjectStoreBase | None = None
        repository_store: RepositoryStoreBase | None = None
        try:
            project_store = create_project_store()
        except Exception as exc:  # noqa: BLE001 - one dead store, not a dead catalog
            logging.warning("Project store unavailable for the project catalog: {}", exc)
        try:
            repository_store = create_repository_store()
        except Exception as exc:  # noqa: BLE001 - one dead store, not a dead catalog
            logging.warning("Repository store unavailable for the project catalog: {}", exc)
        return ProjectCatalog(
            configured=get_config().projects,
            project_store=project_store,
            repository_store=repository_store,
        )

    @staticmethod
    def _resolve_mode(mode: str | None) -> str:
        """Resolve the orchestration mode.

        Only the explicit ``mode`` parameter is honoured — keyword heuristics
        on the user query have been removed because they produced fragile,
        surprising behaviour (accidentally entering plan mode on innocent
        phrasing). Clients must pass ``mode="plan"`` explicitly to opt in.
        """
        if mode in {"plan", "act"}:
            return mode
        return "act"


__all__ = ["Orchestrator"]
