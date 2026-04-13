#!/usr/bin/env python3
"""Session orchestration entrypoint."""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import Callable

from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep, OrchestrationState, Plan, PlanStep, TaskQueue
from meeseeks_core.common import discover_project_instructions, get_logger, session_log_context
from meeseeks_core.components import langfuse_session_context
from meeseeks_core.config import PluginsConfig, get_config, get_config_value
from meeseeks_core.context import ContextBuilder
from meeseeks_core.exit_plan_mode import ensure_plan_dir, plan_file_for
from meeseeks_core.hooks import HookManager, default_hook_manager
from meeseeks_core.hypervisor import AgentHypervisor
from meeseeks_core.permissions import (
    PermissionPolicy,
    approval_callback_from_config,
    load_permission_policy,
)
from meeseeks_core.planning import Planner
from meeseeks_core.session_store import SessionStoreBase, create_session_store
from meeseeks_core.skills import SkillRegistry, activate_skill
from meeseeks_core.token_budget import get_token_budget
from meeseeks_core.tool_registry import ToolRegistry, filter_specs, load_registry
from meeseeks_core.tool_use_loop import ToolUseLoop

logging = get_logger(name="core.orchestrator")

# Longest error blurb to embed in a synthetic closure event. Keeps the
# transcript readable and the downstream ``recent_events`` bullet from
# ballooning the system prompt.
_CLOSURE_ERROR_MAX_LEN = 500


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
    ) -> None:
        """Initialize orchestration dependencies."""
        self._cwd = cwd
        self._session_step_budget = session_step_budget
        self._model_name = (
            model_name
            or get_config_value("llm", "action_plan_model")
            or get_config_value("llm", "default_model", default="gpt-5.2")
        )
        self._fallback_models = (
            fallback_models
            if fallback_models is not None
            else tuple(get_config_value("llm", "fallback_models", default=[]) or [])
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
        plugins_cfg = get_config().plugins

        if plugins_cfg.enabled:
            # Reconcile missing plugins on fresh containers / volume wipes.
            if plugins_cfg.enabled_plugins:
                self._reconcile_missing_plugins(plugins_cfg)

            from pathlib import Path as _Path

            from meeseeks_core.agent_registry import AgentRegistry, parse_agent_file
            from meeseeks_core.hooks import merge_plugin_hooks
            from meeseeks_core.plugins import load_all_plugin_components

            fan_out = load_all_plugin_components()
            self._agent_registry = AgentRegistry()

            for pc in fan_out.components:
                if pc.manifest is None:
                    continue
                plugin_source = f"plugin:{pc.manifest.name}"
                for sd in pc.skill_dirs:
                    self._skill_registry.load_extra_dir(sd, source=plugin_source)
                for cf in pc.command_files:
                    self._skill_registry.load_command_file(cf, source=plugin_source)
                for af in pc.agent_files:
                    agent_def = parse_agent_file(_Path(af), source=plugin_source)
                    if agent_def:
                        self._agent_registry.register(agent_def)
            for hooks_json, plugin_root in fan_out.hooks_configs:
                merge_plugin_hooks(self._hook_manager, hooks_json, plugin_root)
            plugin_mcp_servers = fan_out.mcp_servers
        else:
            plugin_mcp_servers = {}

        self._tool_registry = tool_registry or load_registry(
            cwd=cwd,
            extra_mcp_servers=plugin_mcp_servers or None,
        )
        self._context_builder = ContextBuilder(self._session_store)
        self._planner = Planner(self._tool_registry)

        # Register lossless micro-compaction as a pre_compact hook.
        from meeseeks_core.compaction import micro_compact_events

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
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
        user_id: str | None = None,
        source_platform: str | None = None,
        invocation_id: str | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration for a session."""
        if session_id is None:
            session_id = self._session_store.create_session()

        with session_log_context(session_id):
            with langfuse_session_context(
                session_id,
                user_id=user_id,
                invocation_id=invocation_id,
                source_platform=source_platform,
            ):
                return self._run_with_session_context(
                    user_query,
                    max_iters=max_iters,
                    initial_plan=initial_plan,
                    return_state=return_state,
                    session_id=session_id,
                    mode=mode,
                    should_cancel=should_cancel,
                    allowed_tools=allowed_tools,
                    skill_instructions=skill_instructions,
                    message_queue=message_queue,
                    interrupt_step=interrupt_step,
                )

    def _run_with_session_context(
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
        skill_instructions: str | None = None,
        message_queue: queue.Queue[str] | None = None,
        interrupt_step: threading.Event | None = None,
    ) -> TaskQueue | tuple[TaskQueue, OrchestrationState]:
        """Run orchestration with Langfuse session context set."""
        state = OrchestrationState(goal=user_query, session_id=session_id)
        resolved_mode = self._resolve_mode(mode)
        state.summary = self._session_store.load_summary(session_id)
        state.tool_results = state.tool_results or []
        state.open_questions = state.open_questions or []
        task_queue: TaskQueue | None = None

        self._hook_manager.run_on_session_start(session_id)
        error_msg: str | None = None
        try:
            self._session_store.append_event(
                session_id, {"type": "user", "payload": {"text": user_query}}
            )
            if self._should_update_summary(user_query):
                state.summary = self._update_summary_with_memory(
                    session_id,
                    user_query.strip(),
                )

            updated_summary = self._maybe_auto_compact(session_id)
            if updated_summary:
                state.summary = updated_summary

            if user_query.strip() == "/compact":
                from meeseeks_core.compact import CompactionMode, compact_conversation

                events = self._session_store.load_transcript(session_id)
                result = asyncio.run(compact_conversation(events, CompactionMode.FULL))
                summary = result.summary
                self._session_store.save_summary(session_id, summary)
                state.summary = summary
                state.done = True
                state.done_reason = "compacted"
                task_queue = self._build_direct_response(f"Compaction complete. Summary: {summary}")
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
            if allowed_tools:
                # allowed_tools from frontend scopes MCP tools; built-in tools must stay.
                builtin_ids = [s.tool_id for s in tool_specs if s.kind != "mcp"]
                tool_specs = filter_specs(tool_specs, allowed=allowed_tools + builtin_ids)

            # Skill invocation detection and hot-reload.
            self._skill_registry.maybe_reload()
            if skill_instructions is None:
                _si, _ts = self._try_skill_invocation(user_query, tool_specs)
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
            registry = AgentHypervisor(
                max_concurrent=max_concurrent,
                session_step_budget=self._session_step_budget,
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
            )
            loop = ToolUseLoop(
                agent_context=root_ctx,
                tool_registry=self._tool_registry,
                permission_policy=self._permission_policy,
                approval_callback=self._approval_callback,
                hook_manager=self._hook_manager,
                project_instructions=self._project_instructions,
                skill_instructions=skill_instructions,
                skill_registry=self._skill_registry,
                agent_registry=self._agent_registry,
                cwd=self._cwd,
                session_id=session_id,
            )
            try:
                task_queue, state = asyncio.run(
                    loop.run(
                        user_query,
                        tool_specs=tool_specs,
                        context=context,
                        plan=initial_plan,
                        mode=resolved_mode,
                    )
                )
            finally:
                # Belt-and-suspenders: ensure all agents cleaned up.
                try:
                    asyncio.run(registry.cleanup(timeout=5.0))
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

            completion_payload: dict[str, object] = {
                "done": state.done,
                "done_reason": state.done_reason,
                "task_result": task_queue.task_result,
            }
            if task_queue.last_error:
                completion_payload["error"] = task_queue.last_error
                completion_payload["last_error"] = task_queue.last_error
            self._session_store.append_event(
                session_id,
                {"type": "completion", "payload": completion_payload},
            )

            updated_summary = self._maybe_auto_compact(session_id)
            if updated_summary:
                state.summary = updated_summary

            return (task_queue, state) if return_state else task_queue
        except Exception as exc:
            error_msg = str(exc)
            logging.exception("Orchestration failed for session {}", session_id)
            if task_queue is None:
                task_queue = TaskQueue(_human_message=user_query, action_steps=[])
            task_queue.last_error = str(exc)
            state.done = True
            state.done_reason = "error"
            # Closure marker so the failed turn is always materialised in
            # the UI timeline and the LLM's ``recent_events`` carries
            # narrative closure into the next recovery run.
            self._session_store.append_event(
                session_id,
                {
                    "type": "assistant",
                    "payload": {"text": _format_assistant_closure(state.done_reason, str(exc))},
                },
            )
            self._session_store.append_event(
                session_id,
                {
                    "type": "completion",
                    "payload": {
                        "done": True,
                        "done_reason": state.done_reason,
                        "task_result": task_queue.task_result,
                        "error": str(exc),
                        "last_error": str(exc),
                    },
                },
            )
            return (task_queue, state) if return_state else task_queue
        finally:
            self._hook_manager.run_on_session_end(session_id, error_msg)

    # ------------------------------------------------------------------
    # Session helpers (kept from original)
    # ------------------------------------------------------------------

    def _maybe_generate_title(self, session_id: str) -> None:
        """Kick off title generation in a daemon thread (non-blocking).

        Runs only once per session (guarded by ``load_title`` absence). The
        caller returns immediately; the title appears via a ``title_update``
        event whenever the LLM call finishes. Failures are logged, never
        raised — the first-user-message fallback remains as safety net.
        """
        if self._session_store.load_title(session_id) is not None:
            return
        threading.Thread(
            target=self._run_title_generation,
            args=(session_id,),
            name=f"title-gen-{session_id[:8]}",
            daemon=True,
        ).start()

    def _run_title_generation(self, session_id: str) -> None:
        """Worker body for background title generation."""
        try:
            from meeseeks_core.title_generator import generate_session_title

            events = self._session_store.load_transcript(session_id)
            title = asyncio.run(generate_session_title(events))
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
        from meeseeks_core.token_budget import read_last_input_tokens

        events = self._session_store.load_transcript(session_id)
        events = self._hook_manager.run_pre_compact(events)
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
            from meeseeks_core.compact import CompactionMode, compact_conversation

            result = asyncio.run(compact_conversation(events, CompactionMode.PARTIAL))
            compact_model = result.model or self._model_name
            summary = result.summary
            tokens_saved = result.tokens_saved
        except Exception:
            # Structured compaction failed. Do NOT substitute concatenated raw
            # event text — it would poison the context. Skip this cycle; the
            # next turn will try again.
            logging.warning("Structured compaction failed; skipping cycle", exc_info=True)
            return None
        self._session_store.save_summary(session_id, summary)
        events_summarized = len(events)
        self._session_store.append_event(
            session_id,
            {
                "type": "context_compacted",
                "payload": {
                    "agent_id": None,
                    "depth": 0,
                    "mode": "auto",
                    "model": compact_model,
                    "tokens_before": budget.total_tokens,
                    "tokens_saved": tokens_saved,
                    "tokens_after": budget.total_tokens - tokens_saved,
                    "events_summarized": events_summarized,
                    "summary": summary,
                    "fallback": False,
                },
            },
        )
        self._hook_manager.run_on_compact(
            session_id,
            summary=summary,
            tokens_before=budget.total_tokens,
            tokens_saved=tokens_saved,
            events_summarized=events_summarized,
        )
        return summary

    def _append_action_plan(self, session_id: str, steps: list[PlanStep]) -> None:
        payload_steps = [{"title": step.title, "description": step.description} for step in steps]
        self._session_store.append_event(
            session_id, {"type": "action_plan", "payload": {"steps": payload_steps}}
        )

    @staticmethod
    def _reconcile_missing_plugins(plugins_cfg: PluginsConfig) -> None:
        """Ensure all ``enabled_plugins`` exist in the registry.

        On a fresh container or after a volume wipe, enabled plugins may be
        listed in the config but absent from the registry/cache.  This method
        discovers which are missing and attempts to install them from the
        configured marketplaces.  Errors are logged and skipped — session
        startup should not fail because a plugin couldn't be fetched.
        """
        from meeseeks_core.plugins import (
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

        from meeseeks_core.common import get_logger

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
    ) -> tuple[str | None, list | None]:
        """Detect ``/skill-name args`` in the query and activate the skill.

        Returns ``(skill_instructions, scoped_tool_specs)`` on match,
        or ``(None, None)`` if the query is not a skill invocation.
        """
        query = user_query.strip()
        if not query.startswith("/"):
            return None, None

        parts = query.split(None, 1)
        name = parts[0].lstrip("/")
        args = parts[1] if len(parts) > 1 else ""

        skill = self._skill_registry.get(name)
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
