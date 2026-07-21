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

from mewbo_core.agent_context import AgentContext
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
from mewbo_core.context import ContextBuilder
from mewbo_core.exit_plan_mode import ensure_plan_dir, plan_file_for
from mewbo_core.hooks import HookManager, default_hook_manager
from mewbo_core.hypervisor import ACTIVE_STATUSES, AgentHypervisor
from mewbo_core.permissions import (
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from mewbo_core.run_error import RunError
from mewbo_core.session_provenance import SessionOrigin, TraceProvenance, is_mobile_surface
from mewbo_core.session_store import SessionStoreBase, create_session_store
from mewbo_core.session_tools import SessionTool, SessionToolRegistry
from mewbo_core.skills import SkillRegistry, activate_skill
from mewbo_core.system_instructions import (
    InstructionContext,
    SystemInstructionsStoreBase,
    create_system_instructions_store,
)
from mewbo_core.token_budget import get_token_budget
from mewbo_core.tool_registry import ToolRegistry, ToolSpec, filter_specs, get_or_build_registry
from mewbo_core.tool_use_loop import ToolUseLoop
from mewbo_core.types import CompletionPayload, UserPayload

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
    ) -> None:
        """Initialize orchestration dependencies."""
        self._cwd = cwd
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

            from mewbo_core.agent_registry import AgentRegistry, parse_agent_file
            from mewbo_core.hooks import merge_plugin_hooks
            from mewbo_core.plugins import load_all_plugin_components

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
        self._tool_registry = tool_registry or get_or_build_registry(
            cwd=cwd,
            extra_mcp_servers=plugin_mcp_servers or None,
        )
        self._context_builder = ContextBuilder(self._session_store)

        # Register lossless micro-compaction as a pre_compact hook.
        from mewbo_core.compaction import micro_compact_events

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
        attachments: list[dict] | None = None,
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
                attachments=attachments,
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
        attachments: list[dict] | None = None,
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
                    strict_tool_scope=strict_tool_scope,
                    capability_mode=capability_mode,
                    skill_instructions=skill_instructions,
                    message_queue=message_queue,
                    interrupt_step=interrupt_step,
                    extra_session_tools=extra_session_tools,
                    enable_skills=enable_skills,
                    attachments=attachments,
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
        strict_tool_scope: bool = False,
        capability_mode: str = "all",
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        extra_session_tools: list[SessionTool] | None = None,
        enable_skills: bool = True,
        attachments: list[dict] | None = None,
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
            # Declared as the payload TypedDict, not a loose dict: the event
            # payloads ARE the wire contract, so building to the contract is
            # what keeps a key the schema does not know from reaching the store.
            user_payload: UserPayload = {"text": user_query}
            if attachments:
                # Additive duplicate of the sibling ``context`` event's
                # attachments (see ``context._iter_attachments``) — keeps the
                # exact AttachmentDescriptor dicts so a client can render
                # attachment cards above the user turn, live and on replay,
                # without joining across events.
                user_payload["attachments"] = attachments
            self._session_store.append_event(
                session_id, {"type": "user", "payload": user_payload}
            )
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
            # through ``mewbo_core.commands.execute_command`` so the
            # operation is single-sourced regardless of which UI typed
            # them — CLI ``run_sync`` route, API channel pipeline, or
            # console palette. Per-command rendering still belongs to
            # the calling UI; the orchestrator only short-circuits the
            # tool-use loop and surfaces the rendered ``result.body``.
            stripped_query = user_query.strip()
            parts = stripped_query.split(maxsplit=1)
            if parts and parts[0].startswith("/"):
                cmd_name = parts[0][1:]
                from mewbo_core.commands import (
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
                if strict_tool_scope:
                    # Strict mode: ``allowed_tools`` is authoritative —
                    # nothing outside it survives, not even built-ins.
                    # Used by wiki-qa so the agent doesn't waste round
                    # trips trying ``aider_shell_tool`` / ``spawn_agent``
                    # / etc. Caller is responsible for including any core
                    # tool it actually needs in ``allowed_tools``.
                    tool_specs = filter_specs(
                        tool_specs, allowed=allowed_tools, capability_mode=capability_mode
                    )
                else:
                    # Permissive mode (FE default): ``allowed_tools`` only
                    # scopes MCP tools; built-in tools always stay.
                    builtin_ids = [s.tool_id for s in tool_specs if s.kind != "mcp"]
                    tool_specs = filter_specs(
                        tool_specs,
                        allowed=allowed_tools + builtin_ids,
                        capability_mode=capability_mode,
                    )
            elif capability_mode != "all":
                # No allowlist, but a ROOT capability ceiling applies (a role
                # narrowed a session to ``read_only``). Apply the coarse
                # privilege gate to the registry specs so a read-only session
                # binds only read-tier tools + ``always_load`` — mirroring the
                # per-tier filter a spawned child gets. Guarded on the non-"all"
                # tier so an unrestricted session skips ``filter_specs`` entirely
                # and stays byte-identical (it also never newly applies
                # ``agent.default_denied_tools`` to an unscoped session).
                tool_specs = filter_specs(tool_specs, capability_mode=capability_mode)

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
                from mewbo_core.attestation import AttestationChain

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
                # (unrestricted), and enforcement is separately gated behind
                # agent.workspace_enforcement, so this is a no-op until flipped.
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
                # Whether ``allowed_tools`` is authoritative (strict) or a
                # permissive MCP ceiling — mirrors the ``filter_specs`` branch
                # above so the loop's spawn_agent gate reads the same intent.
                strict_tool_scope=strict_tool_scope,
                cwd=self._cwd,
                session_id=session_id,
                session_capabilities=session_caps,
                extra_session_tools=extra_session_tools,
                enable_skills=enable_skills,
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
            # the ONLY thing that carries that fact out of the run. Omitted when
            # absent, so a run that hit no wall is byte-identical.
            if state.blocked_code:
                completion_payload["blocked_code"] = state.blocked_code
            self._attach_failure_record(completion_payload, task_queue, state.done_reason)
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
            run_error = RunError.from_exception(exc, model=self._model_name)
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
        done_reason: str | None,
    ) -> None:
        """Attach the bounded failure record to a terminal completion payload.

        ``task_queue.last_error`` is a STICKY diagnostic — a mid-run tool
        failure the model recovered from still leaves it set. It is CLAMPED
        whenever set, whatever the outcome: its readers do not check
        ``done_reason`` (the scg map-job persists it onto the job record, the
        CLI prints it on /retry|/continue|/edit), so gating the clamp let a run
        that recovered and finished clean carry a raw multi-KB provider page out
        to them.

        The payload keys used to be WITHHELD whenever ``done_reason ==
        "completed"``, to keep error residue off a successful run's wire. That
        rule cost more than it bought: the runs it silenced were overwhelmingly
        the LAUNDERED ones — a halt presenting as success — and withholding the
        one field able to contradict the status is what turned a wrong status
        into an unfalsifiable one. A status is only worth trusting if the record
        can be used to check it.

        A run that stopped short leaving NO sticky string (a doom-loop halt, a
        spent budget, a failed ground-truth check) is why the structured record
        was populated on so few of the error-ish completions. It gets one here —
        but only the additive ``error_detail``, never the flat keys: those are
        what the legacy clients render as an error card, and a halt that
        produced a wrap-up answer is not an error to put in front of a user.
        """
        if task_queue.last_error:
            run_error = RunError.from_message(task_queue.last_error, model=self._model_name)
            brief = run_error.brief()
            task_queue.last_error = brief
            payload["error"] = brief
            payload["last_error"] = brief
            payload["error_detail"] = run_error.model_dump(mode="json")
        elif done_reason in UNACHIEVED_DONE_REASONS:
            payload["error_detail"] = RunError.from_message(
                f"Run ended without reaching its goal ({done_reason}).",
                model=self._model_name,
            ).model_dump(mode="json")

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
    ) -> tuple[str, ...]:
        """The tool ids an operator's template sees in ``InstructionContext.tools``.

        The ``ToolRegistry`` specs bound for this run, UNIONED with the session
        tools the root agent will actually be built with. The union is the fix:
        the field used to carry the registry specs alone, so every session tool
        (``wiki_*``, ``scg_*``, ``submit_widget``, ``schedule_trigger``) was
        missing and ``'wiki_search' in tools`` silently rendered False for an
        agent that genuinely held it — a template branching on a product tool
        could never fire.

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
                # ``capability_mode`` withholds.
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
            from mewbo_core.title_generator import generate_session_title

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
        from mewbo_core.compact import (
            CompactionMode,
            compact_conversation,
            record_compaction,
        )
        from mewbo_core.token_budget import read_last_input_tokens

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
        from mewbo_core.plugins import (
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
        instructions, scoped_specs = activate_skill(skill, args, tool_specs)
        return instructions, scoped_specs

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
