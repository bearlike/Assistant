#!/usr/bin/env python3
"""Async tool-use conversation loop with sub-agent support."""

from __future__ import annotations

import asyncio
import json
import os
import platform as _platform
import queue as _queue_mod
import re
import time as _time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date as _date
from pathlib import Path
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from mewbo_core.agent_context import AgentContext
from mewbo_core.classes import ActionStep, OrchestrationState, Plan, TaskQueue
from mewbo_core.common import get_git_context, get_logger, get_mock_speaker, get_system_prompt
from mewbo_core.components import (
    build_langfuse_handler,
    langfuse_propagate,
    langfuse_trace_span,
)
from mewbo_core.config import get_config_value, get_version
from mewbo_core.context import ContextSnapshot, render_event_lines
from mewbo_core.exit_plan_mode import (
    SHELL_TOOL_IDS,
    ExitPlanModeTool,
    ensure_plan_dir,
    is_inside_plan_dir,
    is_shell_command_plan_safe,
    plan_file_for,
)
from mewbo_core.hooks import HookManager
from mewbo_core.hypervisor import AgentHandle
from mewbo_core.llm import build_chat_model, specs_to_langchain_tools
from mewbo_core.permissions import PermissionDecision, PermissionPolicy
from mewbo_core.session_tools import (
    DEFAULT_SESSION_TOOL_MODES,
    SessionTool,
    SessionToolRegistry,
)
from mewbo_core.tool_registry import (
    ToolRegistry,
    ToolSpec,
    is_deferred,
)
from mewbo_core.types import Event

logging = get_logger(name="core.tool_use_loop")

# ---------------------------------------------------------------------------
# LLM error classification  (Ref: Codex ContextWindowExceeded skip,
# Claude Code error-classified retry, OpenCode per-provider retry)
# ---------------------------------------------------------------------------


def _classify_llm_error(exc: Exception) -> tuple[bool, float]:
    """Classify an LLM error. Returns ``(should_retry, delay_seconds)``.

    Non-retryable errors (context overflow, auth, bad request) fail
    immediately. Rate-limit errors respect the server's Retry-After header.
    Transient errors get a minimal flat delay.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True, 0.5
    try:
        import litellm.exceptions as litellm_exc  # noqa: I001 — lazy to avoid heavy import at module level
    except ImportError:
        return True, 0.5  # litellm unavailable — default retryable
    if isinstance(exc, litellm_exc.ContextWindowExceededError):
        return False, 0
    if isinstance(
        exc,
        litellm_exc.AuthenticationError | litellm_exc.PermissionDeniedError,
    ):
        return False, 0
    if isinstance(exc, litellm_exc.BadRequestError):
        return False, 0
    if isinstance(exc, litellm_exc.RateLimitError):
        return True, min(_extract_retry_after(exc) or 2.0, 30.0)
    if isinstance(exc, litellm_exc.Timeout):
        return True, 0.5
    if isinstance(
        exc,
        litellm_exc.InternalServerError | litellm_exc.ServiceUnavailableError,
    ):
        return True, 1.0
    if isinstance(exc, litellm_exc.APIConnectionError):
        return True, 0.5
    return True, 0.5  # unknown — retryable, minimal delay


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract Retry-After seconds from a ``RateLimitError``'s httpx response."""
    response = getattr(exc, "response", None)
    if response is not None:
        val = getattr(response, "headers", {}).get("retry-after")
        if val:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return None


def _is_context_overflow(exc: Exception) -> bool:
    """Return True if the exception is a context window exceeded error."""
    try:
        import litellm.exceptions as litellm_exc
    except ImportError:
        return False
    return isinstance(exc, litellm_exc.ContextWindowExceededError)


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

# Matches Claude Code's ``NO_CONTENT_MESSAGE`` (src/constants/messages.ts).
# Empty-string assistant content replayed in history causes extended-thinking
# models (e.g. ``claude-opus-4-6``) to hallucinate framework-style placeholder
# text. Following Claude Code's ``ensureNonEmptyAssistantContent``
# (src/utils/messages.ts), substitute a neutral text block instead of ``""``.
# The placeholder is filtered out of ``agent_message`` events so it never
# reaches the UI.
_NO_CONTENT_PLACEHOLDER = "(no content)"

# Maps tool_id patterns to the AbstractTool operation ("get" or "set").
_OPERATION_SET_KEYWORDS = frozenset(
    {
        "shell",
        "edit",
        "write",
        "create",
        "set",
        "update",
        "delete",
        "apply",
        "remove",
        "patch",
        "insert",
        "append",
        "replace",
        "upload",
        "post",
        "put",
    }
)
_OPERATION_GET_KEYWORDS = frozenset(
    {
        "read",
        "list",
        "search",
        "get",
        "fetch",
        "query",
        "lookup",
        "web_search",
        "web_url_read",
    }
)


@dataclass(frozen=True)
class ToolCallResult:
    """Result of executing a single tool call."""

    tool_call_id: str
    tool_id: str
    content: str
    success: bool


@dataclass
class ToolBatch:
    """A batch of tool calls with shared concurrency mode."""

    calls: list[Any]
    concurrent: bool


@dataclass
class _CachedFileRead:
    """Tracks a file read for dedup detection."""

    path: str  # normalized absolute path
    offset: int
    limit: int | None
    mtime: float  # os.path.getmtime at read time


class ToolUseLoop:
    """Async tool-use conversation loop.

    Each instance owns one conversation with one LLM. Sub-agents are
    created by spawning new ``ToolUseLoop`` instances via the
    ``spawn_agent`` internal tool.
    """

    def __init__(
        self,
        *,
        agent_context: AgentContext,
        tool_registry: ToolRegistry,
        permission_policy: PermissionPolicy,
        approval_callback: Callable[[ActionStep], bool] | None = None,
        hook_manager: HookManager,
        project_instructions: str | None = None,
        skill_instructions: str | None = None,
        skill_registry: Any = None,
        agent_registry: Any = None,
        session_tool_registry: SessionToolRegistry | None = None,
        allowed_tools: list[str] | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        session_capabilities: tuple[str, ...] = (),
    ) -> None:
        """Initialize the tool-use loop.

        Args:
            agent_context: Required — carries model, cancel, logger, registry.
            tool_registry: Registered tools available to this agent.
            permission_policy: Permission rules for tool execution.
            approval_callback: Optional callback for ASK decisions (None for sub-agents).
            hook_manager: Lifecycle hooks.
            project_instructions: CLAUDE.md / AGENTS.md content discovered at session start.
            skill_instructions: Pre-rendered skill body (from user /skill invocation).
            skill_registry: SkillRegistry for auto-invocation catalog + activate_skill handling.
            agent_registry: AgentRegistry for agent type catalog + spawn_agent type lookup.
            session_tool_registry: Registry of plugin-contributed session-tool
                factories.  Each matching factory (filtered by ``allowed_tools``)
                is instantiated for this agent and added to ``self._session_tools``.
            allowed_tools: The agent's allowlist used to filter which session
                tools the plugin registry should build for this agent.  ``None``
                means "no plugin session tools" (root agents get only the
                built-in ``ExitPlanModeTool``).
            cwd: Working directory for this agent (project root).
            session_id: Session identifier — used for plan-mode path scoping.
            session_capabilities: Client-advertised capability tuple from the
                ``X-Mewbo-Capabilities`` header (persisted on the session
                context event). Used to filter capability-gated agents and
                skills out of the system-prompt catalogs and ``activate_skill``
                / ``spawn_agent`` lookups.
        """
        self._ctx = agent_context
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._project_instructions = project_instructions
        self._skill_instructions = skill_instructions
        self._skill_registry = skill_registry
        self._agent_registry = agent_registry
        self._session_tool_registry = session_tool_registry
        self._cwd = cwd
        self._session_id = session_id
        self._session_capabilities = session_capabilities

        # Dedup cache for read_file: prevents redundant reads when the
        # same file + range hasn't changed on disk (mtime check).
        self._file_read_cache: dict[str, _CachedFileRead] = {}

        # Plan-mode state (mutable across the loop's lifetime).
        self._current_mode: str = "act"
        # Authoritative token count from the most recent LLM response's
        # usage_metadata.input_tokens. Zero until the first call lands.
        self._last_input_tokens: int = 0

        # Create SpawnAgentTool when this agent can spawn children.
        self._spawn_agent_tool: Any = None
        if agent_context.can_spawn:
            from mewbo_core.spawn_agent import SpawnAgentTool

            # Ref: [DeepMind-Delegation §4.7] Sub-agents inherit parent's approval
            # policy so they can execute write/edit/shell tools.
            self._spawn_agent_tool = SpawnAgentTool(
                agent_context=agent_context,
                tool_registry=tool_registry,
                permission_policy=permission_policy,
                approval_callback=approval_callback,
                hook_manager=hook_manager,
                project_instructions=project_instructions,
                cwd=cwd,
                agent_registry=agent_registry,
                session_tool_registry=session_tool_registry,
                session_capabilities=session_capabilities,
            )

        # Assemble session tools — per-agent stateful handlers that carry
        # their own schema, dispatch, and run-termination flag. The core's
        # built-in ``ExitPlanModeTool`` is always attached to root agents
        # with a session id; plugin-contributed tools are filtered through
        # the agent's ``allowed_tools`` allowlist.
        self._session_tools: list[SessionTool] = []
        if agent_context.depth == 0 and session_id is not None:
            self._session_tools.append(
                ExitPlanModeTool(
                    session_id=session_id,
                    event_logger=agent_context.event_logger,
                )
            )
        if session_tool_registry is not None and session_id is not None:
            self._session_tools.extend(
                session_tool_registry.build_for(
                    allowed_tools,
                    session_id=session_id,
                    event_logger=agent_context.event_logger,
                )
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        user_query: str,
        *,
        tool_specs: list[ToolSpec],
        context: ContextSnapshot | None = None,
        plan: Plan | None = None,
        mode: str = "act",
    ) -> tuple[TaskQueue, OrchestrationState]:
        """Run the async tool-use loop and return TaskQueue + OrchestrationState."""
        state = OrchestrationState(goal=user_query)
        # Plan-mode is enforced via: (1) filtered tool schema at bind time,
        # (2) path-scoped permission check on edits, (3) the exit_plan_mode
        # approval gate. The loop flips ``_current_mode`` to ``"act"`` after
        # the user approves a plan and re-binds tools.
        self._current_mode = mode if mode in {"plan", "act"} else "act"
        if self._current_mode == "plan" and self._session_id is not None:
            state.plan_path = plan_file_for(self._session_id)
            ensure_plan_dir(self._session_id)
        # Propagate plan context so children inherit session and mode.
        if self._spawn_agent_tool is not None:
            self._spawn_agent_tool.session_id = self._session_id
            self._spawn_agent_tool.parent_mode = self._current_mode
        executed_steps: list[ActionStep] = []
        tool_outputs: list[str] = []
        last_error: str | None = None
        final_response: str | None = None

        # Register self in the hypervisor registry.
        # Reuse the handle created by SpawnAgentTool when one already exists for
        # this agent_id — avoids overwriting it and losing the reference held by
        # the lifecycle manager (which later stores AgentResult on the handle).
        existing = await self._ctx.registry.get(self._ctx.agent_id)
        if existing is not None:
            handle = existing
        else:
            handle = AgentHandle(
                agent_id=self._ctx.agent_id,
                parent_id=self._ctx.parent_id,
                depth=self._ctx.depth,
                model_name=self._ctx.model_name,
                task_description=user_query[:200],
            )
            await self._ctx.registry.register(handle)
        handle.status = "running"

        # Ref: [AgentCgroup §4.2] Background watchdog for stall detection.
        # Code-level reflex — zero token cost. Root only.
        watchdog_task: asyncio.Task[None] | None = None
        if self._ctx.depth == 0:
            watchdog_task = asyncio.create_task(self._watchdog())

        # Langfuse context managers — initialized in try, cleaned in finally.
        agent_span: Any = None
        _agent_span_cm: Any = None
        _propagate_cm: Any = None

        try:
            # Ref: [DeepMind-Delegation §4.5] Global eye for root agent
            agent_tree = ""
            if self._ctx.depth == 0:
                agent_tree = await self._ctx.registry.render_agent_tree(
                    exclude_agent_id=self._ctx.agent_id,
                )
            # Deferred-tool partitioning. When ``agent.tool_search.mode`` is
            # 'on', MCP / metadata.deferred specs are stripped from the
            # initial bind and surfaced by name only via the
            # ``<available-deferred-tools>`` block. The model fetches the
            # schemas it needs through ``tool_search``; the per-turn re-bind
            # hook below grows the bound list as tools are discovered.
            self._tool_search_enabled = self._is_tool_search_enabled()
            self._tool_specs_full = list(tool_specs)
            self._deferred_ids = (
                {s.tool_id for s in tool_specs if is_deferred(s)}
                if self._tool_search_enabled
                else set()
            )
            active_specs = self._select_active_specs(tool_specs, discovered=set())
            messages = self._build_messages(
                user_query,
                context,
                plan,
                agent_tree=agent_tree,
            )
            tool_schemas = self._build_tool_schemas_for_mode(
                active_specs,
                self._current_mode,
            )
            model = self._bind_model(tool_schemas)
            self._last_active_ids = {s.tool_id for s in active_specs}

            langfuse_handler = build_langfuse_handler(
                user_id="mewbo-tool-use",
                session_id=f"tool-use-{self._ctx.agent_id}",
                trace_name="mewbo-tool-use",
                version=get_version(),
                release=get_config_value("runtime", "envmode", default="Not Specified"),
            )
            invoke_config: dict[str, Any] = {}
            if langfuse_handler is not None:
                invoke_config["callbacks"] = [langfuse_handler]
                metadata = getattr(langfuse_handler, "langfuse_metadata", None)
                if isinstance(metadata, dict) and metadata:
                    invoke_config["metadata"] = metadata

            # -- Langfuse: agent-level span + attribute propagation --------
            _agent_role = "root" if self._ctx.depth == 0 else f"child-{self._ctx.agent_id[:8]}"
            _agent_span_name = f"agent:{_agent_role}"
            _agent_span_cm = langfuse_trace_span(
                _agent_span_name,
                metadata={
                    "agentid": self._ctx.agent_id[:12],
                    "model": self._ctx.model_name,
                    "depth": str(self._ctx.depth),
                    "mode": self._current_mode,
                },
                input_data={"task": user_query[:200]},
            )
            agent_span = _agent_span_cm.__enter__()
            _propagate_cm = langfuse_propagate(
                tags=[
                    "mewbo-tool-use",
                    f"model:{self._ctx.model_name}",
                    f"depth:{self._ctx.depth}",
                ]
            )
            _propagate_cm.__enter__()

            turns = 0
            compacted_this_turn = False
            while not state.done:
                compacted_this_turn = False
                # Check cancellation.
                if self._ctx.should_cancel is not None and self._ctx.should_cancel():
                    state.done = True
                    state.done_reason = "canceled"
                    break

                # Check for interrupt (root agent only).
                if self._ctx.interrupt_step is not None and self._ctx.interrupt_step.is_set():
                    self._ctx.interrupt_step.clear()
                    messages.append(
                        HumanMessage(content="[System: Current step interrupted by user.]")
                    )

                # Drain any queued user steering messages (root agent only).
                if self._ctx.message_queue is not None:
                    while not self._ctx.message_queue.empty():
                        try:
                            msg = self._ctx.message_queue.get_nowait()
                            messages.append(HumanMessage(content=msg))
                        except _queue_mod.Empty:
                            break

                with langfuse_trace_span(
                    f"step:{turns}",
                    metadata={
                        "turn": str(turns),
                        "model": self._ctx.model_name,
                    },
                ) as span:
                    if span is not None:
                        try:
                            span.update_trace(input={"turn": turns, "message_count": len(messages)})
                        except Exception:
                            pass

                    # Ref: [AgentCgroup §4.2] Graduated enforcement — NL feedback, not kill.
                    # Inject budget warning so the agent can adapt its strategy.
                    if self._ctx.registry.budget_exhausted():
                        messages.append(
                            SystemMessage(
                                content="BUDGET WARNING: Session step budget exhausted. "
                                "Summarize your current findings and return results immediately."
                            )
                        )

                    llm_timeout = float(
                        get_config_value(
                            "agent",
                            "llm_call_timeout",
                            default=60.0,
                        )
                    )
                    llm_max_retries = int(
                        get_config_value(
                            "agent",
                            "llm_call_retries",
                            default=2,
                        )
                    )
                    # Heartbeat events so clients (console/CLI) can distinguish
                    # "waiting on LLM" from a silent hang.
                    self._emit_event(
                        {
                            "type": "llm_call_start",
                            "payload": {
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": turns,
                            },
                        }
                    )
                    response: AIMessage | None = None
                    last_exc: Exception | None = None
                    non_retryable = False
                    models_to_try = [self._ctx.model_name, *self._ctx.fallback_models]
                    active_model = model  # Already bound primary

                    for model_idx, try_model_name in enumerate(models_to_try):
                        is_fallback = model_idx > 0
                        if is_fallback:
                            if non_retryable:
                                break  # Don't cascade non-retryable errors
                            active_model = build_chat_model(
                                model_name=try_model_name,
                            ).bind_tools(tool_schemas)
                            self._emit_event(
                                {
                                    "type": "llm_retry",
                                    "payload": {
                                        "agent_id": self._ctx.agent_id,
                                        "depth": self._ctx.depth,
                                        "step": turns,
                                        "attempt": 0,
                                        "max_attempts": 1,
                                        "error": str(last_exc)[:200],
                                        "fallback_to": try_model_name,
                                    },
                                }
                            )
                            logging.warning(
                                "Falling back to %s (%s)",
                                try_model_name,
                                str(last_exc)[:100],
                            )

                        retries = llm_max_retries if not is_fallback else 1
                        for attempt in range(1, retries + 1):
                            try:
                                response = await asyncio.wait_for(
                                    active_model.ainvoke(messages, config=invoke_config or None),
                                    timeout=llm_timeout,
                                )
                                _usage = getattr(response, "usage_metadata", None)
                                if _usage:
                                    _h = await self._ctx.registry.get(self._ctx.agent_id)
                                    if _h:
                                        _h.input_tokens += _usage.get("input_tokens", 0)
                                        _h.output_tokens += _usage.get("output_tokens", 0)
                                    # Authoritative signal for compaction: what
                                    # the API just said this prompt consumed.
                                    self._last_input_tokens = int(
                                        _usage.get("input_tokens", 0) or 0
                                    )
                                break
                            except (asyncio.TimeoutError, Exception) as exc:
                                last_exc = exc
                                should_retry, delay = _classify_llm_error(exc)
                                if not should_retry:
                                    # Reactive fallback: compact on context overflow.
                                    _compact_info = (
                                        await self._compact_messages(messages)
                                        if _is_context_overflow(exc) and not compacted_this_turn
                                        else None
                                    )
                                    if _compact_info:
                                        compacted_this_turn = True
                                        await self._ctx.registry.record_compaction(
                                            self._ctx.agent_id,
                                        )
                                        self._emit_event(
                                            {
                                                "type": "context_compacted",
                                                "payload": {
                                                    **_compact_info,
                                                    "agent_id": self._ctx.agent_id,
                                                    "depth": self._ctx.depth,
                                                    "mode": "reactive",
                                                    "turn": turns,
                                                },
                                            }
                                        )
                                        continue  # Retry with compacted messages
                                    non_retryable = True
                                    break  # Don't cascade non-retryable errors
                                if attempt == retries:
                                    break  # Next model or final failure
                                self._emit_event(
                                    {
                                        "type": "llm_retry",
                                        "payload": {
                                            "agent_id": self._ctx.agent_id,
                                            "depth": self._ctx.depth,
                                            "step": turns,
                                            "attempt": attempt,
                                            "max_attempts": retries,
                                            "error": str(exc)[:200],
                                            "delay": delay,
                                            "retryable": should_retry,
                                        },
                                    }
                                )
                                logging.warning(
                                    "LLM call attempt {}/{} failed ({}), retrying in {:.1f}s",
                                    attempt,
                                    retries,
                                    str(exc)[:100],
                                    delay,
                                )
                                if delay > 0:
                                    await asyncio.sleep(delay)
                        if response is not None:
                            break  # Success

                    if response is None:
                        self._emit_event(
                            {
                                "type": "llm_call_end",
                                "payload": {
                                    "agent_id": self._ctx.agent_id,
                                    "depth": self._ctx.depth,
                                    "step": turns,
                                },
                            }
                        )
                        # Enrich Langfuse span with structured error info.
                        if span is not None:
                            try:
                                span.update(
                                    level="ERROR",
                                    status_message=str(last_exc)[:500],
                                    metadata={
                                        "errortype": type(last_exc).__name__
                                        if last_exc
                                        else "Unknown",
                                        "models_tried": ",".join(models_to_try),
                                    },
                                )
                            except Exception:
                                pass
                        raise RuntimeError(
                            f"LLM call failed on all models "
                            f"({', '.join(models_to_try)}): "
                            f"{last_exc}"
                        ) from last_exc
                    assert response is not None  # guaranteed by loop or raise
                    _step_usage = getattr(response, "usage_metadata", None)
                    _h_ref = await self._ctx.registry.get(self._ctx.agent_id)
                    # LangChain ``UsageMetadata`` exposes provider cache and
                    # reasoning subtotals (Anthropic + OpenAI normalised):
                    #   input_token_details.cache_creation — written to cache
                    #     this call (Anthropic 5-min: 1.25× input price)
                    #   input_token_details.cache_read — served from cache
                    #     (Anthropic: 0.1× input; OpenAI: 0.5× input)
                    #   output_token_details.reasoning — extended-thinking /
                    #     o1 hidden tokens (billed as output)
                    # Capturing them per call lets clients show fresh-vs-
                    # cached breakdown and an honest billable signal that
                    # accounts for cache discounts.
                    _in_det = _step_usage.get("input_token_details") or {} if _step_usage else {}
                    _out_det = _step_usage.get("output_token_details") or {} if _step_usage else {}
                    self._emit_event(
                        {
                            "type": "llm_call_end",
                            "payload": {
                                "agent_id": self._ctx.agent_id,
                                "depth": self._ctx.depth,
                                "step": turns,
                                "input_tokens": (
                                    _step_usage.get("input_tokens", 0) if _step_usage else 0
                                ),
                                "output_tokens": (
                                    _step_usage.get("output_tokens", 0) if _step_usage else 0
                                ),
                                "cache_creation_input_tokens": int(
                                    _in_det.get("cache_creation", 0) or 0
                                ),
                                "cache_read_input_tokens": int(_in_det.get("cache_read", 0) or 0),
                                "reasoning_output_tokens": int(_out_det.get("reasoning", 0) or 0),
                                "cumulative_input_tokens": (_h_ref.input_tokens if _h_ref else 0),
                                "cumulative_output_tokens": (_h_ref.output_tokens if _h_ref else 0),
                            },
                        }
                    )
                    # Strip thinking blocks from the response before appending
                    # to the conversation history.  Anthropic requires a
                    # ``signature`` field on thinking blocks when replayed,
                    # but proxies (LiteLLM) may not preserve it.
                    raw = getattr(response, "content", None)
                    if isinstance(raw, list):
                        sanitized = [
                            block
                            for block in raw
                            if not (isinstance(block, dict) and block.get("type") == "thinking")
                        ]
                        # Claude Code's ``ensureNonEmptyAssistantContent``
                        # (src/utils/messages.ts:4933): never leave
                        # empty-string content. Empty assistant turns in
                        # history cause extended-thinking models to
                        # hallucinate framework-style placeholders.
                        if not sanitized:
                            sanitized = [{"type": "text", "text": _NO_CONTENT_PLACEHOLDER}]
                        response = AIMessage(
                            content=sanitized,
                            tool_calls=response.tool_calls,
                            additional_kwargs=response.additional_kwargs,
                            usage_metadata=response.usage_metadata,
                            id=response.id,
                        )
                    elif response.tool_calls and (not raw or not str(raw).strip()):
                        # The proxy (LiteLLM) strips thinking blocks itself
                        # and returns ``content=""`` (a STRING, not a list).
                        # Without this branch, the empty string survives
                        # sanitisation and gets replayed in history, causing
                        # the model to hallucinate placeholder meta-text.
                        response = AIMessage(
                            content=_NO_CONTENT_PLACEHOLDER,
                            tool_calls=response.tool_calls,
                            additional_kwargs=response.additional_kwargs,
                            usage_metadata=response.usage_metadata,
                            id=response.id,
                        )
                    messages.append(response)

                    if not response.tool_calls:
                        # Text response — task is done.
                        content = self._extract_text_content(getattr(response, "content", ""))
                        final_response = content
                        tool_outputs.append(content)
                        state.done = True
                        state.done_reason = "completed"
                        break

                    # Emit intermediate text as agent_message for trace logs.
                    text_content = self._extract_text_content(getattr(response, "content", ""))
                    if text_content:
                        self._emit_event(
                            {
                                "type": "agent_message",
                                "payload": {
                                    "text": text_content,
                                    "agent_id": self._ctx.agent_id,
                                    "depth": self._ctx.depth,
                                },
                            }
                        )

                    # Execute tool calls with concurrency-aware partitioning.
                    # Exclusive tools run alone; concurrent-safe tools are gathered.
                    specs_map = {s.tool_id: s for s in tool_specs}
                    batches = self._partition_tool_calls(response.tool_calls, specs_map)
                    results: list[ToolCallResult] = []
                    for batch in batches:
                        if batch.concurrent:
                            batch_results = await asyncio.gather(
                                *[self._safe_execute(tc, tool_specs) for tc in batch.calls],
                            )
                            results.extend(batch_results)
                        else:
                            result = await self._safe_execute(batch.calls[0], tool_specs)
                            results.append(result)

                    for tool_call, result in zip(response.tool_calls, results):
                        messages.append(
                            ToolMessage(
                                content=result.content,
                                tool_call_id=result.tool_call_id,
                            )
                        )
                        tool_outputs.append(f"{result.tool_id}: {result.content}")
                        if not result.success:
                            last_error = result.content
                        # Track as ActionStep for TaskQueue compatibility.
                        action_step = self._tool_call_to_action_step(tool_call)
                        mock = get_mock_speaker()
                        action_step.result = mock(content=result.content)
                        executed_steps.append(action_step)

                    # Episodic plan-mode: a session tool (e.g. exit_plan_mode)
                    # signals the loop to terminate so the thread exits
                    # cleanly. Approval/rejection happens out-of-band via
                    # SessionRuntime. Materialise the list so every tool's
                    # flag is consumed — ``any`` short-circuits and would
                    # leave a second tool's flag set for the next turn.
                    term_flags = [t.should_terminate_run() for t in self._session_tools]
                    if any(term_flags):
                        state.done = True
                        state.done_reason = "awaiting_approval"
                        break

                    # Update registry step count.
                    await self._ctx.registry.update_step(
                        self._ctx.agent_id,
                        results[-1].tool_id if results else "",
                    )
                    turns += 1

                    # Ref: [DeepMind-Delegation §4.5] Auto-update progress
                    # for parent monitoring. Zero token cost — direct write.
                    if self._ctx.depth > 0 and results:
                        last = results[-1]
                        note = f"turn {turns}: {last.tool_id}"
                        snippet = last.content[:100] if last.content else ""
                        if last.success:
                            note += f" -> {snippet}"
                        else:
                            note += f" -> FAILED: {snippet}"
                        handle = await self._ctx.registry.get(
                            self._ctx.agent_id,
                        )
                        if handle:
                            handle.progress_note = note

                    # Inject failure feedback so the model can adapt.
                    failures = [r for r in results if not r.success]
                    if failures:
                        messages.append(
                            SystemMessage(
                                content=f"{len(failures)}/{len(results)} tool call(s)"
                                " failed this step — adapt your approach."
                            )
                        )

                    # Re-bind newly discovered deferred tools. Discovery is
                    # derived from the message history each turn, so this is
                    # compaction-resilient: whatever survives compaction
                    # still drives the bound set on the next iteration.
                    if self._tool_search_enabled and self._deferred_ids:
                        discovered = self._discovered_from_messages(messages)
                        active_specs = self._select_active_specs(
                            self._tool_specs_full, discovered=discovered
                        )
                        new_active_ids = {s.tool_id for s in active_specs}
                        if new_active_ids != self._last_active_ids:
                            tool_schemas = self._build_tool_schemas_for_mode(
                                active_specs, self._current_mode
                            )
                            model = self._bind_model(tool_schemas)
                            self._last_active_ids = new_active_ids

                    # Proactive mid-loop compaction check.
                    if self._should_compact_messages(messages):
                        _compact_info = await self._compact_messages(messages)
                        if _compact_info:
                            await self._ctx.registry.record_compaction(
                                self._ctx.agent_id,
                            )
                            self._emit_event(
                                {
                                    "type": "context_compacted",
                                    "payload": {
                                        **_compact_info,
                                        "agent_id": self._ctx.agent_id,
                                        "depth": self._ctx.depth,
                                        "mode": "mid_loop",
                                        "turn": turns,
                                    },
                                }
                            )

                    if span is not None:
                        try:
                            span.update_trace(
                                output={
                                    "tool_calls": len(response.tool_calls),
                                    "turns": turns,
                                }
                            )
                        except Exception:
                            pass

            # Safety net: currently unreachable (all loop exits set
            # state.done=True), but retained as defensive code for future
            # exit paths that may break without setting state.done.
            if not state.done and final_response is None and messages:
                # Ref: [DeepMind-Delegation §4.5] Inject child results at synthesis.
                # Root may have non-blocking children still running — give them
                # a brief grace period, then include available results.
                if self._ctx.depth == 0:
                    running = await self._ctx.registry.collect_running(
                        self._ctx.agent_id,
                    )
                    if running:
                        await asyncio.sleep(2.0)
                    completed = await self._ctx.registry.collect_completed(
                        self._ctx.agent_id,
                    )
                    if completed:
                        result_lines = []
                        for h in completed:
                            r = h.result
                            if r:
                                result_lines.append(
                                    f"[{h.agent_id[:8]}] {r.status}: {r.summary or r.content[:300]}"
                                )
                        if result_lines:
                            messages.append(
                                SystemMessage(
                                    content="Completed sub-agent results:\n"
                                    + "\n".join(result_lines),
                                )
                            )
                    still_running = await self._ctx.registry.collect_running(
                        self._ctx.agent_id,
                    )
                    if still_running:
                        ids = ", ".join(h.agent_id[:8] for h in still_running)
                        messages.append(
                            SystemMessage(
                                content=f"WARNING: {len(still_running)} agent(s) "
                                f"still running ({ids}). Include their partial "
                                "progress in your synthesis.",
                            )
                        )

                try:
                    messages.append(
                        SystemMessage(
                            content=(
                                "You MUST now provide your final answer based on "
                                "all the information gathered so far. "
                                "Do NOT call any more tools. Respond with text only."
                            )
                        )
                    )
                    # Invoke without tool bindings to prevent further tool calls.
                    unbound = build_chat_model(model_name=self._ctx.model_name)
                    synthesis_timeout = float(
                        get_config_value("agent", "llm_call_timeout", default=60.0)
                    )
                    synthesis: AIMessage = await asyncio.wait_for(
                        unbound.ainvoke(messages, config=invoke_config or None),
                        timeout=synthesis_timeout,
                    )
                    _usage = getattr(synthesis, "usage_metadata", None)
                    if _usage:
                        _h = await self._ctx.registry.get(self._ctx.agent_id)
                        if _h:
                            _h.input_tokens += _usage.get("input_tokens", 0)
                            _h.output_tokens += _usage.get("output_tokens", 0)
                    final_response = self._extract_text_content(getattr(synthesis, "content", ""))
                except Exception:
                    logging.warning("Final synthesis turn failed.", exc_info=True)

                # Fallback: if synthesis produced nothing, build a minimal
                # response from successful tool outputs so the user isn't
                # left with an empty result.
                if not final_response or not final_response.strip():
                    successful = [o for o in tool_outputs if o and not o.startswith("ERROR")]
                    if successful:
                        final_response = "Partial results:\n\n" + "\n\n".join(successful[-5:])

            if not state.done:
                state.done = True
                state.done_reason = "completed"

            # Build TaskQueue for compatibility with CLI / API consumers.
            plan_steps = list(plan.steps) if plan and plan.steps else []
            task_queue = TaskQueue(
                plan_steps=plan_steps,
                action_steps=executed_steps,
            )
            # task_result is the LLM's final synthesized text only.
            task_queue.task_result = (final_response or "").strip()
            task_queue.last_error = last_error
            state.tool_results = tool_outputs

        finally:
            # Close Langfuse agent span and propagation context.
            if agent_span is not None:
                try:
                    agent_span.update(
                        output={
                            "total_steps": turns,
                            "done_reason": state.done_reason or "unknown",
                        }
                    )
                except Exception:
                    pass
            if _propagate_cm is not None:
                try:
                    _propagate_cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - defensive
                    pass
            if _agent_span_cm is not None:
                try:
                    _agent_span_cm.__exit__(None, None, None)
                except Exception:  # pragma: no cover - defensive
                    pass

            # Stop the background watchdog.
            if watchdog_task is not None and not watchdog_task.done():
                watchdog_task.cancel()

            # Wait for lifecycle managers to complete cleanup.
            if self._spawn_agent_tool is not None:
                await self._spawn_agent_tool.await_lifecycle_managers(timeout=3.0)

            # Cleanup: cancel any child agents still running.
            children = await self._ctx.registry.list_children(self._ctx.agent_id)
            for child in children:
                if child.status == "running":
                    await self._ctx.registry.cancel_agent(child.agent_id)

            await self._ctx.registry.mark_done(
                self._ctx.agent_id,
                "completed" if state.done else "failed",
            )

        return task_queue, state

    # ------------------------------------------------------------------
    # Error-isolated task wrapper
    # ------------------------------------------------------------------

    def _get_tool_timeout(self, tool_name: str) -> float:
        """Get timeout for a tool. Uses spec.timeout, falls back to 120s."""
        spec = self._tool_registry.get_spec(tool_name) if self._tool_registry else None
        if spec:
            return spec.timeout
        return 120.0

    def _partition_tool_calls(
        self, tool_calls: list[Any], specs_map: dict[str, ToolSpec]
    ) -> list[ToolBatch]:
        """Group consecutive concurrent-safe tools; isolate exclusive tools."""
        batches: list[ToolBatch] = []
        current_concurrent: list[Any] = []
        for tc in tool_calls:
            spec = specs_map.get(tc.get("name", ""))
            is_safe = spec.concurrency_safe if spec else True
            if is_safe:
                current_concurrent.append(tc)
            else:
                if current_concurrent:
                    batches.append(ToolBatch(calls=list(current_concurrent), concurrent=True))
                    current_concurrent = []
                batches.append(ToolBatch(calls=[tc], concurrent=False))
        if current_concurrent:
            batches.append(ToolBatch(calls=list(current_concurrent), concurrent=True))
        return batches

    async def _safe_execute(
        self,
        tool_call: Any,
        tool_specs: list[ToolSpec],
    ) -> ToolCallResult:
        """Execute a tool call with timeout.

        Catches exceptions so gather does not cancel siblings.
        """
        tool_name = tool_call.get("name", "")
        timeout = self._get_tool_timeout(tool_name)
        try:
            return await asyncio.wait_for(
                self._execute_tool_call(tool_call, tool_specs),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            error_msg = f"Tool '{tool_name}' timed out after {timeout}s"
            logging.error(error_msg)
            return ToolCallResult(
                tool_call_id=tool_call.get("id", ""),
                tool_id=tool_name,
                content=f"ERROR: {error_msg}",
                success=False,
            )
        except asyncio.CancelledError:
            raise  # Must propagate for TaskGroup cancellation.
        except Exception as exc:
            return ToolCallResult(
                tool_call_id=tool_call.get("id", ""),
                tool_id=tool_name,
                content=f"ERROR: {exc}",
                success=False,
            )

    # ------------------------------------------------------------------
    # Message construction
    # ------------------------------------------------------------------

    def _build_messages(
        self,
        user_query: str,
        context: ContextSnapshot | None,
        plan: Plan | None,
        agent_tree: str = "",
    ) -> list[BaseMessage]:
        """Build the initial message list for the conversation."""
        system_parts: list[str] = [get_system_prompt("system")]

        # Environment context.
        work_dir = self._cwd or str(Path.cwd())
        env_lines = [
            "# Environment",
            f"- Working directory: {work_dir}",
            f"- Platform: {_platform.system().lower()}",
            f"- Date: {_date.today().isoformat()}",
            f"- Mewbo version: {get_version()}",
        ]
        system_parts.append("\n".join(env_lines))

        # Project instructions (CLAUDE.md / AGENTS.md).
        if self._project_instructions:
            system_parts.append(f"Project instructions:\n{self._project_instructions}")

        # Ref: [DeepMind-Delegation §4.5] Root agent's global eye — live agent tree
        if agent_tree:
            system_parts.append(f"# Active agent tree\n{agent_tree}")

        # Git context (injected after project instructions).
        git_ctx = get_git_context(self._cwd)
        if git_ctx:
            system_parts.append(f"# Git Context\n{git_ctx}")

        # Active skill instructions (from user /skill invocation).
        if self._skill_instructions:
            system_parts.append(f"Active skill instructions:\n{self._skill_instructions}")

        # Auto-invocable skills catalog (for LLM-driven activation).
        if self._skill_registry is not None:
            catalog = self._skill_registry.render_catalog(self._session_capabilities)
            if catalog:
                system_parts.append(catalog)

        # Registered agent types catalog (for spawn_agent agent_type selection).
        if self._agent_registry is not None:
            agent_catalog = self._agent_registry.render_catalog(self._session_capabilities)
            if agent_catalog:
                system_parts.append(agent_catalog)

        # Session context.
        if context and context.summary:
            system_parts.append(f"Session summary:\n{context.summary}")
        if context and context.recent_events:
            rendered = render_event_lines(context.recent_events)
            if rendered:
                system_parts.append(f"Recent conversation:\n{rendered}")

        # Attached file contents.
        if context and context.attachment_texts:
            system_parts.append("Attached files:\n" + "\n---\n".join(context.attachment_texts))

        # Tool-specific guidance from prompt files.
        tool_guidance = self._render_tool_guidance()
        if tool_guidance:
            system_parts.append(f"Tool guidance:\n{tool_guidance}")

        # Deferred-tool catalog (names only). Schemas are fetched on demand
        # by the model via the ``tool_search`` tool; the per-turn re-bind in
        # ``run()`` then makes the matched tools invocable.
        deferred_block = self._render_deferred_tool_block()
        if deferred_block:
            system_parts.append(deferred_block)

        # Plan context.
        if plan and plan.steps:
            plan_lines = "\n".join(
                f"{i + 1}. {s.title} — {s.description}" for i, s in enumerate(plan.steps)
            )
            system_parts.append(
                f"Execute this plan:\n{plan_lines}\n"
                "Follow steps in order. Adapt if results require it."
            )

        # Depth-aware sub-agent guidance.
        system_parts.append(self._build_depth_guidance())

        # Plan-mode reminder — injected when the loop was started in plan
        # mode. Rendered with the session-scoped plan path and the shell
        # command allowlist so the model knows exactly what it may write
        # and which shell commands it is permitted to run.
        if self._current_mode == "plan" and self._session_id is not None:
            if self._ctx.depth == 0:
                # Root hypervisor: automata prompt + plan path for review.
                try:
                    hyper_template = get_system_prompt("plan_hypervisor")
                except OSError:
                    hyper_template = ""
                if hyper_template:
                    plan_path = plan_file_for(self._session_id)
                    system_parts.append(f"{hyper_template}\n\nPlan file: {plan_path}")
            else:
                # Plan sub-agent: full plan-mode prompt with placeholders.
                try:
                    template = get_system_prompt("plan_mode_reminder")
                except OSError:
                    template = ""
                if template:
                    plan_path = plan_file_for(self._session_id)
                    shell_allowlist = self._plan_mode_shell_allowlist()
                    if shell_allowlist:
                        bullets = "\n".join(f"    - `{entry}`" for entry in shell_allowlist)
                    else:
                        bullets = "    - (none — shell is disabled in plan mode)"
                    rendered = template.replace("{plan_path}", plan_path).replace(
                        "{shell_allowlist_bullets}", bullets
                    )
                    system_parts.append(rendered)

        system_prompt = "\n\n".join(p for p in system_parts if p)
        # If the active context carries images for a vision-capable model,
        # build a multipart HumanMessage that interleaves the user's text
        # with ``image_url`` parts (LiteLLM/OpenAI Chat Completions format).
        # Otherwise stick with plain-string content to keep the cache
        # prefix friendly.
        image_parts = list(getattr(context, "attachment_images", []) or []) if context else []
        if image_parts:
            # langchain's HumanMessage expects ``list[str | dict]`` (invariant);
            # widen the element type so mypy accepts mixed text/image parts.
            human_content: list[str | dict] = [
                {"type": "text", "text": user_query},
                *image_parts,
            ]
            return [SystemMessage(content=system_prompt), HumanMessage(content=human_content)]
        return [SystemMessage(content=system_prompt), HumanMessage(content=user_query)]

    def _build_depth_guidance(self) -> str:
        """Build delegation-lifecycle-aware prompt guidance.

        Ref: [DeepMind-Delegation §4.1] Contract-first decomposition — root
        agents define verifiable acceptance criteria for sub-tasks.
        Ref: [CoA §3.2] Manager/worker role separation — root synthesizes,
        sub-agents execute.
        Ref: [Aletheia §3] Verification by same model in different role
        prevents confirmation bias.
        Ref: [DeepMind-Delegation §4.7] Liability firebreaks at chain boundaries.
        """
        depth = self._ctx.depth
        max_depth = self._ctx.max_depth
        remaining = self._ctx.remaining_depth
        is_root = depth == 0
        is_leaf = not self._ctx.can_spawn

        if is_root:
            # Ref: [CoA §3.2] Root = manager/hypervisor.
            # Ref: [DeepMind-Delegation §4.4] Non-blocking delegation protocol.
            plan_mode = self._current_mode == "plan"
            if plan_mode:
                lines = [
                    f"# Agent role: Root hypervisor — plan mode (depth {depth}/{max_depth})",
                    "",
                    "## Goal: produce an approved plan via a sub-agent",
                    "- A plan sub-agent explores the codebase and drafts the plan file.",
                    "- You orchestrate: spawn it, monitor progress, propose the result.",
                    "- exit_plan_mode submits the plan for user approval.",
                    "- The plan is complete only when the user approves it.",
                    "- If the sub-agent fails, spawn a new one"
                    " — you cannot write the plan yourself.",
                ]
            else:
                lines = [
                    f"# Agent role: Root hypervisor (depth {depth}/{max_depth})",
                    "",
                    "## Default: Direct execution",
                    "- Handle tasks directly using your tools. Most tasks do NOT need sub-agents.",
                    "- Simple operations (write a file, run a command, search, read)"
                    " — do them yourself.",
                    "- Sequential tasks (write then run then read) — do them yourself, in order.",
                    "- Only spawn sub-agents for genuinely parallel, independent work.",
                    "",
                    "## When to spawn (rare)",
                    "- Multiple independent tasks that benefit from running concurrently.",
                    "- Each sub-task must be self-contained with clear acceptance_criteria.",
                    "- Scope sub-agents with allowed_tools/denied_tools.",
                ]
            # Shared root sections: delegation protocol, safety, synthesis,
            # system awareness, when to stop — apply in both plan and act mode.
            lines.extend(
                [
                    "",
                    "## Async delegation protocol (when you spawn)",
                    "- spawn_agent returns immediately with {agent_id, status: 'submitted'}.",
                    "- Continue with independent work while children execute in background.",
                    "- React to '[Agent xxx finished: ...]' notifications between your steps.",
                    "- Call check_agents to see tree state and collect completed results.",
                    "- Call check_agents(wait=true) when you have no independent work left.",
                    "- Use steer_agent to inject context or course-correct running agents.",
                    "- Do NOT call check_agents in a loop — trust notifications. (epoll, not poll)",
                    "",
                    "## Safety",
                    "- steer_agent(action='cancel') stops a stuck or misbehaving agent.",
                    "- A background watchdog warns stalled agents automatically"
                    " (2min+ no progress).",
                    "",
                    "## Synthesize",
                    "- When all children complete, collect results via check_agents.",
                    "- Verify results against acceptance_criteria before trusting them.",
                    "- Check 'status' before using:"
                    " completed=reliable, failed/cannot_solve=handle.",
                    "",
                    "## System awareness",
                    "- You operate within a bounded environment with intentional guardrails.",
                    "- CWD restrictions, permission denials, and tool scope limits"
                    " are non-negotiable.",
                    "- If a tool or sub-agent reports a restriction, adapt — do NOT retry blindly.",
                    "",
                    "## When to stop",
                    "- If the same operation fails twice, do not retry it a third time.",
                    "- If a sub-agent fails, do not spawn another sub-agent for the same task.",
                    "- Report what failed, why, and what you tried — then let the user decide.",
                ]
            )
        elif is_leaf:
            # Ref: [DeepMind-Delegation §4.7] Liability firebreak at leaf
            lines = [
                f"# Agent role: Leaf executor (depth {depth}/{max_depth})",
                "You are a delegated sub-agent with a bounded task.",
                "",
                "## Execution protocol",
                "- Complete your assigned task directly using available tools.",
                "- Do NOT attempt to delegate — you cannot spawn sub-agents.",
                "- When done, provide a clear, structured summary of what you accomplished.",
                "- Your text response (without tool calls) signals task completion"
                " and ends your execution.",
                "",
                "## Failure handling",
                "- If you cannot complete the task, say so explicitly with the reason.",
                "- If a tool reports a restriction, stop and report it"
                " — do not attempt workarounds.",
                "- If an operation fails twice, report failure instead of retrying.",
                "- Do NOT spin or retry endlessly — admit failure so the parent can adapt.",
            ]
        else:
            # Sub-orchestrator: can delegate but has bounded scope
            lines = [
                f"# Agent role: Sub-orchestrator (depth {depth}/{max_depth}, "
                f"{remaining} levels remaining)",
                "You are a delegated sub-agent that can further delegate.",
                "",
                "## Execution protocol",
                "- Focus on your assigned task scope — do not expand beyond it.",
                "- Prefer direct tool use. Only spawn child agents for independent parallel work.",
                "- Verify child agent results before incorporating them.",
                "- Return a structured summary when your task is complete.",
                "- Your text response (without tool calls) signals task completion"
                " and ends your execution.",
                "",
                "## Failure handling",
                "- If you cannot complete the task, say so explicitly with the reason.",
                "- If a tool reports a restriction or boundary, stop and report to your parent.",
                "- Do NOT retry failed operations or attempt workarounds for system limits.",
            ]
            if remaining <= 2:
                # Ref: [DeepMind-Delegation §4.7] Approaching delegation boundary
                lines.append(
                    "\nDELEGATION BOUNDARY: You are deep in the agent tree. "
                    "Prefer direct tool use over spawning."
                )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Background watchdog  (Ref: [AgentCgroup §4.2])
    # ------------------------------------------------------------------

    async def _watchdog(self) -> None:
        """Code-level safety monitor — zero token cost.

        Periodically checks for stalled agents and injects NL warnings.
        Runs only for the root agent as a background asyncio task.

        Ref: [AgentCgroup §4.2] Graduated enforcement via NL injection.
        Ref: [DeepMind-Delegation §4.4] Internal trigger: delegatee
        unresponsive → diagnose → intervene.
        """
        try:
            while True:
                await asyncio.sleep(30)
                stalled = await self._ctx.registry.stalled_agents(threshold=120.0)
                for h in stalled:
                    await self._ctx.registry.send_message(
                        h.agent_id,
                        "STALL WARNING: No progress for 2+ minutes. Wrap up or report status.",
                    )
                    if self._ctx.message_queue is not None:
                        self._ctx.message_queue.put_nowait(
                            f"[Watchdog: Agent {h.agent_id[:8]} stalled on {h.last_tool_id}]",
                        )
        except asyncio.CancelledError:
            pass  # Normal shutdown path.

    def _render_tool_guidance(self) -> str:
        """Render tool-specific prompt guidance for local tools."""
        prompts: list[str] = []
        for spec in self._tool_registry.list_specs():
            if spec.kind != "local" or not spec.prompt_path:
                continue
            try:
                prompt = get_system_prompt(spec.prompt_path)
            except OSError:
                continue
            if prompt:
                prompts.append(prompt)
        return "\n\n".join(prompts)

    # ------------------------------------------------------------------
    # Skill activation (internal tool handler)
    # ------------------------------------------------------------------

    def _handle_activate_skill(self, action_step: ActionStep) -> Any:
        """Handle an ``activate_skill`` tool call from the LLM.

        Returns the skill body as a mock result so it arrives as a
        ``ToolMessage`` — the LLM reads the instructions and follows them.
        """
        from mewbo_core.skills import activate_skill

        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        skill_name = str(args.get("skill_name", ""))
        skill_args = str(args.get("args", ""))

        registry = self._skill_registry
        skill = (
            registry.get(skill_name, self._session_capabilities) if registry else None
        )
        if skill is None:
            msg = f"ERROR: Unknown skill '{skill_name}'"
            return type("R", (), {"content": msg})()
        if skill.disable_model_invocation:
            msg = f"ERROR: Skill '{skill_name}' is user-invocable only"
            return type("R", (), {"content": msg})()

        instructions, _ = activate_skill(skill, skill_args)
        logging.info("LLM auto-activated skill '{}'", skill_name)
        body = f"## Skill: {skill_name}\n\n{instructions}\n\nFollow these instructions now."
        return type("R", (), {"content": body})()

    # ------------------------------------------------------------------
    # Model binding
    # ------------------------------------------------------------------

    def _configured_edit_tool_id(self) -> str:
        """Return the tool_id for the edit tool appropriate for the active model.

        Prefers model-derived capability detection (via
        ``llm.model_prefers_structured_patch``).  The ``agent.edit_tool``
        config value is honoured as an explicit override when non-empty.
        """
        from mewbo_core.llm import model_prefers_structured_patch

        # Explicit user override always wins
        override = get_config_value("agent", "edit_tool", default="")
        if override == "structured_patch":
            return "file_edit_tool"
        if override == "search_replace_block":
            return "aider_edit_block_tool"

        # Derive from model identity
        model_name: str | None = getattr(self._ctx, "model_name", None)
        if model_prefers_structured_patch(model_name):
            return "file_edit_tool"
        return "aider_edit_block_tool"

    def _plan_mode_shell_allowlist(self) -> list[str]:
        """Return the configured shell command prefix allowlist for plan mode.

        Read from ``agent.plan_mode_shell_allowlist``. An empty list means
        the shell tool is disabled in plan mode.
        """
        raw = get_config_value("agent", "plan_mode_shell_allowlist", default=[])
        if isinstance(raw, list):
            return [str(item).strip() for item in raw if str(item).strip()]
        if isinstance(raw, str):
            return [entry.strip() for entry in raw.split(",") if entry.strip()]
        return []

    def _plan_mode_allow_mcp(self) -> bool:
        """Return True when MCP tools are permitted in plan mode.

        Read from ``agent.plan_mode_allow_mcp``. Defaults to True (matches
        Claude Code's permissive behaviour for user-configured MCP servers).
        """
        raw = get_config_value("agent", "plan_mode_allow_mcp", default=True)
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        return bool(raw)

    # ------------------------------------------------------------------
    # Mid-loop context compaction
    # ------------------------------------------------------------------

    def _should_compact_messages(self, messages: list[BaseMessage]) -> bool:
        """Token-reality check anchored on the last response's usage_metadata.

        ``messages`` is intentionally unused — the LLM's ``usage_metadata``
        already reflects everything it saw (system prompt, tool schemas,
        all messages). No char-count estimation, no manual overhead.
        """
        del messages  # Signature preserved for callers; data comes from the API.
        if self._last_input_tokens <= 0:
            return False  # No LLM call yet; nothing authoritative to check.
        from mewbo_core.token_budget import get_model_max_input_tokens

        max_input = get_model_max_input_tokens(self._ctx.model_name)
        threshold = float(get_config_value("token_budget", "auto_compact_threshold", default=0.8))
        return self._last_input_tokens >= max_input * threshold

    async def _compact_messages(
        self,
        messages: list[BaseMessage],
    ) -> dict[str, Any] | None:
        """Compact the in-flight message list by summarizing older messages.

        Keeps messages[0] (system prompt) and the last ``recent_keep``
        messages, summarizing everything in between via the structured
        compaction prompt.  Returns a dict with ``summary`` and
        ``events_summarized`` on success, or ``None`` if skipped.
        """
        recent_keep = 6  # ~3 turn pairs (AI + Tool)
        if len(messages) <= recent_keep + 2:
            return None  # Not enough to compact

        to_summarize = messages[1:-recent_keep]
        kept_tail = messages[-recent_keep:]

        # Build text representation for the summarizer.
        lines: list[str] = []
        for m in to_summarize:
            role = getattr(m, "type", "unknown")
            text = m.content if isinstance(m.content, str) else str(m.content)
            lines.append(f"[{role}] {text[:2000]}")
        summary_input = "\n".join(lines)

        # If root agent, include agent tree so delegation context survives.
        if self._ctx.depth == 0:
            tree = await self._ctx.registry.render_agent_tree(
                exclude_agent_id=self._ctx.agent_id,
            )
            if tree:
                summary_input += f"\n\n# Active agent tree at compaction:\n{tree}"

        # Invoke the compaction LLM with priority-ordered model fallback.
        from mewbo_core.compact import (
            _extract_summary,
            get_compact_prompt,
            resolve_compact_models,
        )

        _compact_models = resolve_compact_models(self._ctx.model_name)
        _compact_model = _compact_models[0]
        _msgs = [
            SystemMessage(content=get_compact_prompt()),
            HumanMessage(content=f"Summarize this conversation:\n\n{summary_input}"),
        ]
        response = None
        for _i, _candidate in enumerate(_compact_models):
            _compact_model = _candidate
            try:
                llm = build_chat_model(model_name=_compact_model)
                response = await llm.ainvoke(_msgs)
                break
            except Exception:
                if _i < len(_compact_models) - 1:
                    logging.warning(
                        "Mid-loop compact model %s failed, trying next: %s",
                        _compact_model,
                        _compact_models[_i + 1],
                        exc_info=True,
                    )
                else:
                    logging.warning("Mid-loop compaction LLM call failed", exc_info=True)
                    return None

        if response is None:
            return None  # all models failed

        # Capture compaction LLM tokens on the agent handle.
        _usage = getattr(response, "usage_metadata", None)
        if _usage:
            _h = await self._ctx.registry.get(self._ctx.agent_id)
            if _h:
                _h.input_tokens += _usage.get("input_tokens", 0)
                _h.output_tokens += _usage.get("output_tokens", 0)

        raw = response.content if hasattr(response, "content") else str(response)
        if isinstance(raw, list):
            raw = next(
                (b["text"] for b in raw if isinstance(b, dict) and b.get("type") == "text"),
                "",
            )
        summary = _extract_summary(raw)
        events_summarized = len(to_summarize)

        # Rebuild messages in-place.
        system_msg = messages[0]
        messages.clear()
        messages.append(system_msg)
        messages.append(SystemMessage(content=f"[Compacted context]\n{summary}"))
        messages.extend(kept_tail)
        return {
            "summary": summary,
            "events_summarized": events_summarized,
            "model": _compact_model,
        }

    def _is_tool_search_enabled(self) -> bool:
        """Return True if the deferred-tool / on-demand-schema feature is on.

        Read fresh from config so the field can be flipped without a
        process restart. Sub-orchestrators inherit by reading the same
        ``agent.tool_search.mode`` value.
        """
        return str(get_config_value("agent", "tool_search", "mode", default="off")) == "on"

    def _select_active_specs(
        self,
        specs: list[ToolSpec],
        *,
        discovered: set[str],
    ) -> list[ToolSpec]:
        """Return the spec subset to bind on the model this turn.

        ``non_deferred ∪ {tool_search} ∪ (deferred ∩ discovered)``. When
        deferred-loading is off, returns ``specs`` unchanged. The same
        function drives both the run-start bind and the per-turn re-bind
        so there is exactly one source of truth for what is bound.
        """
        if not self._tool_search_enabled or not self._deferred_ids:
            return list(specs)
        keep: list[ToolSpec] = []
        for spec in specs:
            if spec.tool_id in self._deferred_ids and spec.tool_id not in discovered:
                continue
            keep.append(spec)
        return keep

    # Match each ``<function>{...}</function>`` line emitted by ToolSearchRunner.
    # The runner serialises ``{"name": ..., "description": ..., "parameters": {...}}``
    # with ``json.dumps`` so ``"name"`` is always the first field; anchoring there
    # avoids brace-counting through the nested ``parameters`` schema.
    _DISCOVERED_FUNC_RE = re.compile(r'<function>\s*\{\s*"name"\s*:\s*"([^"]+)"')

    def _discovered_from_messages(self, messages: list[BaseMessage]) -> set[str]:
        """Scan message history for tool names exposed by past tool_search calls.

        Discovery is derived from messages — no separate state — so the
        set survives compaction unchanged: whatever messages remain after
        compaction still parse the same way. Only ``ToolMessage`` content
        is scanned; the regex matches the ``<function>...</function>``
        line format produced by ``ToolSearchRunner``.
        """
        if not self._deferred_ids:
            return set()
        names: set[str] = set()
        for msg in messages:
            if not isinstance(msg, ToolMessage):
                continue
            content = msg.content
            if isinstance(content, str):
                for match in self._DISCOVERED_FUNC_RE.finditer(content):
                    name = match.group(1)
                    if name in self._deferred_ids:
                        names.add(name)
        return names

    def _render_deferred_tool_block(self) -> str:
        """Render a compact ``<available-mcp-servers>`` system-prompt section.

        Lists MCP servers with their tool counts (one line) plus any
        non-MCP deferred tool ids. Server names — not full tool ids — keep
        the prompt tight even when many MCP tools are connected; the model
        searches by keyword (``tool_search`` + server name or capability)
        rather than scanning a long flat list.
        """
        if not getattr(self, "_tool_search_enabled", False):
            return ""
        deferred_ids: set[str] = getattr(self, "_deferred_ids", set())
        if not deferred_ids:
            return ""
        deferred_specs = [s for s in self._tool_specs_full if s.tool_id in deferred_ids]

        servers: dict[str, int] = {}
        other: list[str] = []
        for spec in deferred_specs:
            if spec.kind == "mcp":
                server = str(spec.metadata.get("server") or "unknown")
                servers[server] = servers.get(server, 0) + 1
            else:
                other.append(spec.tool_id)

        parts: list[str] = []
        if servers:
            summary = ", ".join(f"{name} ({n})" for name, n in sorted(servers.items()))
            parts.append(f"<available-mcp-servers>{summary}</available-mcp-servers>")
        if other:
            parts.append(f"Other deferred tools: {', '.join(sorted(other))}.")
        parts.append(
            "Schemas are not loaded — call `tool_search` with keywords "
            "(e.g. server name, action) or `select:<tool_id>` to load them "
            "before invoking."
        )
        return "\n".join(parts)

    def _build_tool_schemas_for_mode(
        self,
        specs: list[ToolSpec],
        mode: str,
    ) -> list[dict[str, Any]]:
        """Return langchain tool schemas appropriate for ``mode``.

        In plan mode the schema is filtered to: read-only tools + the
        configured edit tool (path-scoped at the permission layer) + the
        shell tool (command-allowlisted at the permission layer, iff the
        allowlist is non-empty) + MCP tools (iff
        ``agent.plan_mode_allow_mcp`` is True). In act mode all specs pass
        through.
        """
        if mode != "plan":
            return specs_to_langchain_tools(specs)
        edit_tool_id = self._configured_edit_tool_id()
        shell_enabled = bool(self._plan_mode_shell_allowlist())
        allow_mcp = self._plan_mode_allow_mcp()
        filtered = [
            spec
            for spec in specs
            if spec.read_only
            or (spec.tool_id == edit_tool_id and self._ctx.depth > 0)
            or (shell_enabled and spec.tool_id in SHELL_TOOL_IDS)
            or (allow_mcp and spec.kind == "mcp")
        ]
        return specs_to_langchain_tools(filtered)

    def _bind_model(self, tool_schemas: list[dict[str, Any]]) -> Any:
        """Build a chat model and bind tool schemas."""
        model = build_chat_model(model_name=self._ctx.model_name)
        plan_mode = self._current_mode == "plan"
        # In plan mode, the root (depth=0) gets agent management tools so it
        # can spawn and monitor the plan sub-agent. Non-root plan agents get
        # no agent tools — they explore and draft only.
        plan_root = plan_mode and self._ctx.depth == 0
        if (not plan_mode or plan_root) and self._spawn_agent_tool is not None:
            from mewbo_core.spawn_agent import SPAWN_AGENT_SCHEMA

            tool_schemas = [*tool_schemas, SPAWN_AGENT_SCHEMA]
            # Ref: [DeepMind-Delegation §4.4] Root-only management tools
            # for non-blocking agent monitoring and steering.
            if self._ctx.depth == 0:
                from mewbo_core.spawn_agent import (
                    CHECK_AGENTS_SCHEMA,
                    STEER_AGENT_SCHEMA,
                )

                tool_schemas = [*tool_schemas, CHECK_AGENTS_SCHEMA, STEER_AGENT_SCHEMA]
        # Inject activate_skill schema when auto-invocable skills exist.
        if (
            not plan_mode
            and self._skill_registry is not None
            and self._skill_registry.list_auto_invocable(self._session_capabilities)
        ):
            from mewbo_core.skills import ACTIVATE_SKILL_SCHEMA

            tool_schemas = [*tool_schemas, ACTIVATE_SKILL_SCHEMA]
        # Bind session-tool schemas only for tools whose ``modes`` include
        # the current orchestration mode. Data-driven — no tool_id string
        # match. Plugin tools missing the attribute default to act-mode.
        current_mode = "plan" if plan_mode else "act"
        for session_tool in self._session_tools:
            tool_modes = getattr(session_tool, "modes", None) or DEFAULT_SESSION_TOOL_MODES
            if current_mode in tool_modes:
                tool_schemas = [*tool_schemas, session_tool.schema]
        if tool_schemas:
            return model.bind_tools(tool_schemas)
        return model

    # ------------------------------------------------------------------
    # Tool execution (async)
    # ------------------------------------------------------------------

    async def _execute_tool_call(
        self,
        tool_call: Any,
        tool_specs: list[ToolSpec],
    ) -> ToolCallResult:
        """Execute a single LLM tool_call: permission → hooks → run → emit event."""
        tool_call_id: str = tool_call.get("id") or ""
        tool_id: str = tool_call.get("name") or ""

        action_step = self._tool_call_to_action_step(tool_call)

        # MCP input coercion.
        spec = self._tool_registry.get_spec(tool_id)
        if spec is not None:
            coercion_error = _coerce_mcp_tool_input(action_step, spec)
            if coercion_error:
                self._emit_tool_result_event(action_step, None, error=coercion_error)
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {coercion_error}",
                    success=False,
                )

        # Permission check.
        if not self._check_permission(action_step):
            # If the permission branch wrote a detailed error to
            # ``action_step.result`` (e.g., plan-mode path scoping), use
            # that as the tool-result content so the model can self-correct.
            denial_content = getattr(action_step.result, "content", None) or "Permission denied"
            self._emit_tool_result_event(action_step, None, error=str(denial_content))
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                content=str(denial_content),
                success=False,
            )

        # Pre-tool hook.
        action_step = self._hook_manager.run_pre_tool_use(action_step)

        # File read dedup: return a stub if this file was already read
        # with the same params and hasn't changed on disk.
        if tool_id == "read_file":
            dedup_stub = self._check_file_read_cache(action_step)
            if dedup_stub is not None:
                self._emit_tool_result_event(action_step, dedup_stub)
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=dedup_stub,
                    success=True,
                )

        # Execute — internal tools (spawn_agent, session tools, activate_skill)
        # first, then the registry.
        session_tool = next(
            (t for t in self._session_tools if t.tool_id == tool_id), None
        )
        if tool_id == "spawn_agent" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.run_async(action_step)
            except Exception as exc:
                logging.error("spawn_agent failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "check_agents" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.handle_check_agents(action_step)
            except Exception as exc:
                logging.error("check_agents failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "steer_agent" and self._spawn_agent_tool is not None:
            try:
                result = await self._spawn_agent_tool.handle_steer_agent(action_step)
            except Exception as exc:
                logging.error("steer_agent failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif session_tool is not None:
            try:
                result = await session_tool.handle(action_step)
            except Exception as exc:
                logging.error("session tool {} failed: {}", tool_id, exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )
        elif tool_id == "activate_skill" and self._skill_registry is not None:
            result = self._handle_activate_skill(action_step)
        else:
            tool = self._tool_registry.get(tool_id)
            if tool is None:
                self._emit_tool_result_event(action_step, None, error="Tool not available")
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content="ERROR: Tool not available",
                    success=False,
                )
            try:
                # Prefer async execution for tools that support it (MCP tools).
                # Falls back to to_thread for sync-only tools (aider_*, etc.).
                if hasattr(tool, "arun"):
                    result = await tool.arun(action_step)
                else:
                    result = await asyncio.to_thread(tool.run, action_step)
            except Exception as exc:
                logging.error("Tool execution failed: {}", exc)
                self._emit_tool_result_event(action_step, None, error=str(exc))
                return ToolCallResult(
                    tool_call_id=tool_call_id,
                    tool_id=tool_id,
                    content=f"ERROR: {exc}",
                    success=False,
                )

        # Post-tool hook.
        result = self._hook_manager.run_post_tool_use(action_step, result)

        content = getattr(result, "content", None)
        if content is None:
            content = "" if result is None else str(result)
        content_str = str(content) if not isinstance(content, str) else content
        spec = self._tool_registry.get_spec(tool_id)
        max_chars = spec.max_result_chars if spec else 2000
        if isinstance(content, dict):
            # Truncate large text fields inside the dict before serializing,
            # so the JSON envelope (metadata like exit_code, duration_ms) is
            # always preserved even when stdout/stderr are huge.
            if max_chars:
                truncated = dict(content)
                for field in ("stdout", "stderr", "output", "result", "content", "text"):
                    val = truncated.get(field)
                    if isinstance(val, str) and len(val) > max_chars:
                        truncated[field] = val[:max_chars] + "\n[truncated]"
                content = truncated
            content_str = json.dumps(content, ensure_ascii=False, default=str)

        # Micro-compaction: strip ANSI escapes.
        content_str = _ANSI_ESCAPE_RE.sub("", content_str)

        # Snapshot for the event — preserves the JSON envelope so the
        # frontend can always parse structured fields (exit_code, duration_ms).
        # Use a generous event-side cap (decoupled from `max_chars`, which is
        # the LLM-context cap): the frontend can scroll the full output, and
        # `result_file` still backstops pathological results when an export dir
        # is configured. Without this, MCP tool responses get truncated to
        # 2000 chars in the UI even though the full content exists in memory.
        event_str = content_str
        EVENT_MAX_CHARS = 100_000
        if len(event_str) > EVENT_MAX_CHARS:
            event_str = event_str[:EVENT_MAX_CHARS] + "\n[truncated — see result_file]"

        # Save large results to file when export dir is configured.
        result_file: str | None = None
        export_dir = str(get_config_value("runtime", "result_export_dir", default="") or "")
        if export_dir and max_chars and len(content_str) > max_chars:
            try:
                export_path = Path(export_dir)
                export_path.mkdir(parents=True, exist_ok=True)
                fid = tool_call_id or f"{tool_id}-{int(_time.time() * 1000)}"
                safe_id = re.sub(r"[^\w\-]", "_", fid)
                result_path = export_path / f"{safe_id}.txt"
                result_path.write_text(content_str, encoding="utf-8")
                result_file = str(result_path)
                content_str = (
                    f"[Full output ({len(content_str)} chars) saved to {result_file}. "
                    f"Read the file for complete content.]"
                )
            except OSError:
                pass  # Fall through to normal truncation

        # Final truncation for the LLM (safety net).
        if max_chars and len(content_str) > max_chars:
            content_str = content_str[:max_chars] + "\n[truncated]"

        # Populate file read cache after successful read.
        if tool_id == "read_file":
            self._populate_file_read_cache(action_step)

        # Invalidate file read cache when a file is edited.
        if tool_id in ("file_edit_tool", "aider_edit_block_tool"):
            edit_args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            edited_path = str(edit_args.get("file_path", "") or edit_args.get("path", ""))
            if edited_path:
                norm = os.path.normpath(edited_path)
                self._file_read_cache.pop(norm, None)
                # Passive LSP diagnostics: surface errors after edits.
                content_str = _append_lsp_feedback(
                    content_str,
                    norm,
                    self._cwd or "",
                )

        self._emit_tool_result_event(action_step, event_str, result_file=result_file)
        return ToolCallResult(
            tool_call_id=tool_call_id,
            tool_id=tool_id,
            content=content_str,
            success=True,
        )

    # ------------------------------------------------------------------
    # ActionStep construction
    # ------------------------------------------------------------------

    def _tool_call_to_action_step(self, tool_call: Any) -> ActionStep:
        """Convert an LLM tool_call dict to an ActionStep."""
        tool_id: str = tool_call.get("name") or ""
        args: Any = tool_call.get("args") or {}
        # Inject session cwd as `root` for registered local tools (aider-style
        # file/shell tools consume it via `argument.get("root")`). Unregistered
        # tools — session tools, spawn_agent, activate_skill — use strict
        # schemas that reject stray keys, so they opt out by not being here.
        if self._cwd and isinstance(args, dict) and "root" not in args:
            spec = self._tool_registry.get_spec(tool_id)
            if spec is not None and spec.kind != "mcp":
                args = {**args, "root": self._cwd}
        operation = _infer_operation(tool_id)
        return ActionStep(
            title=tool_id,
            tool_id=tool_id,
            operation=operation,
            tool_input=args,
        )

    # ------------------------------------------------------------------
    # File-read dedup cache
    # ------------------------------------------------------------------

    def _check_file_read_cache(self, action_step: ActionStep) -> str | None:
        """Return a stub if this file was already read with the same params, else ``None``."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        path = str(args.get("path", ""))
        if not path:
            return None
        root = str(args.get("root") or "")
        offset = int(args.get("offset", 0) or 0)
        limit = args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None

        try:
            joined = os.path.join(root, path) if root else path
            full_path = os.path.normpath(joined)
        except (TypeError, ValueError):
            return None

        cached = self._file_read_cache.get(full_path)
        if cached is None:
            return None
        if cached.offset != offset or cached.limit != limit:
            return None

        # Check mtime — file may have been edited externally.
        try:
            current_mtime = os.path.getmtime(full_path)
        except OSError:
            return None
        if current_mtime != cached.mtime:
            del self._file_read_cache[full_path]
            return None

        return (
            "File unchanged since last read. The content from the earlier "
            "Read tool_result in this conversation is still current — "
            "refer to that instead of re-reading."
        )

    def _populate_file_read_cache(self, action_step: ActionStep) -> None:
        """Record a successful file read for future dedup."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        path = str(args.get("path", ""))
        if not path:
            return
        root = str(args.get("root") or "")
        offset = int(args.get("offset", 0) or 0)
        limit = args.get("limit")
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                limit = None
        try:
            joined = os.path.join(root, path) if root else path
            full_path = os.path.normpath(joined)
            mtime = os.path.getmtime(full_path)
        except (TypeError, ValueError, OSError):
            return
        self._file_read_cache[full_path] = _CachedFileRead(
            path=full_path,
            offset=offset,
            limit=limit,
            mtime=mtime,
        )

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def _check_permission(self, action_step: ActionStep) -> bool:
        """Check permission for an action step. Returns True if allowed."""
        # Plan-mode gating is authoritative: read-only tools, the scoped
        # edit tool, and exit_plan_mode are allowed; everything else is
        # denied. The normal approval policy is bypassed so that plan-mode
        # exploration does not get blocked by ASK rules.
        if self._current_mode == "plan":
            return self._plan_mode_permission(action_step)

        decision = self._permission_policy.decide(action_step)
        decision = self._hook_manager.run_permission_request(action_step, decision)
        if decision == PermissionDecision.ASK:
            approved = self._approval_callback(action_step) if self._approval_callback else False
            decision = PermissionDecision.ALLOW if approved else PermissionDecision.DENY
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": action_step.tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": decision.value,
                    },
                }
            )
        if decision == PermissionDecision.DENY:
            mock = get_mock_speaker()
            action_step.result = mock(content=f"Permission denied for {action_step.tool_id}.")
            return False
        return True

    def _plan_mode_permission(self, action_step: ActionStep) -> bool:
        """Plan-mode permission branch: allow read-only + path-scoped edits.

        Returns True if the action is allowed in plan mode (and normal
        policy checks should also run), or False to deny outright. The
        denial path sets an actionable error on ``action_step.result`` and
        emits a permission event so the model and user see the refusal.
        """
        tool_id = action_step.tool_id
        # Always allow internal tools that signal loop termination.
        if tool_id == "exit_plan_mode":
            return True
        # Root (depth=0) can use agent management tools to spawn and
        # monitor the plan sub-agent.  Non-root plan agents cannot.
        _AGENT_MGMT_TOOLS = {"spawn_agent", "check_agents", "steer_agent"}
        if tool_id in _AGENT_MGMT_TOOLS and self._ctx.depth == 0:
            return True
        spec = self._tool_registry.get_spec(tool_id)
        # Read-only tools are unrestricted in plan mode.
        if spec is not None and spec.read_only:
            return True
        # The configured edit tool is allowed ONLY when its target path
        # resolves inside the session's plan directory.
        edit_tool_id = self._configured_edit_tool_id()
        if tool_id == edit_tool_id and self._session_id is not None:
            args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            candidate = str(args.get("file_path", "") or "")
            if candidate and is_inside_plan_dir(candidate, self._session_id):
                return True
            attempted = candidate or "<missing file_path>"
            plan_path = plan_file_for(self._session_id)
            msg = (
                f"Plan mode: edits restricted to {plan_path}. You attempted "
                f"to write {attempted}. Write only to the plan file, then "
                "call exit_plan_mode."
            )
            mock = get_mock_speaker()
            action_step.result = mock(content=msg)
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": "deny",
                    },
                }
            )
            return False
        # Shell tool: permitted only when the command matches an allowlisted
        # prefix AND contains no shell metacharacters. The denial message
        # includes the allowlist so the model can self-correct in one turn.
        shell_allowlist = self._plan_mode_shell_allowlist()
        if tool_id in SHELL_TOOL_IDS and shell_allowlist:
            args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
            command = str(args.get("command", "") or "").strip()
            if is_shell_command_plan_safe(command, shell_allowlist):
                return True
            allowed_preview = ", ".join(shell_allowlist)
            attempted = command or "<missing command>"
            plan_hint = ""
            if self._session_id is not None:
                if self._ctx.depth == 0:
                    plan_hint = (
                        " You cannot write the plan directly. Spawn a sub-agent to draft it."
                    )
                else:
                    plan_hint = (
                        f" To write the plan, use your edit tool on "
                        f"{plan_file_for(self._session_id)}."
                    )
            msg = (
                f"Plan mode: shell command blocked. You attempted: `{attempted}`. "
                f"Allowed prefixes: {allowed_preview}. No pipes, redirects, "
                f"`&&`/`;`, `$VAR` expansion, or backticks.{plan_hint}"
            )
            mock = get_mock_speaker()
            action_step.result = mock(content=msg)
            self._emit_event(
                {
                    "type": "permission",
                    "payload": {
                        "tool_id": tool_id,
                        "operation": action_step.operation,
                        "tool_input": action_step.tool_input,
                        "decision": "deny",
                    },
                }
            )
            return False
        # User-enabled MCP tools: blanket allow under the config flag. Matches
        # Claude Code's permissive behaviour and trusts the user's mcp.json.
        if self._plan_mode_allow_mcp() and spec is not None and spec.kind == "mcp":
            return True
        # Everything else (agent tools for non-root, shell when allowlist
        # empty, MCP when flag is False, hallucinated tool names) is denied.
        plan_hint = ""
        if self._session_id is not None:
            if self._ctx.depth == 0:
                plan_hint = " You cannot write the plan directly. Spawn a sub-agent to draft it."
            else:
                plan_hint = f" Plan file: {plan_file_for(self._session_id)}."
        mock = get_mock_speaker()
        action_step.result = mock(
            content=(
                f"Plan mode: tool '{tool_id}' is unavailable. Use read-only "
                "tools to explore and your edit tool to draft the plan, "
                f"then call exit_plan_mode.{plan_hint}"
            )
        )
        self._emit_event(
            {
                "type": "permission",
                "payload": {
                    "tool_id": tool_id,
                    "operation": action_step.operation,
                    "tool_input": action_step.tool_input,
                    "decision": "deny",
                },
            }
        )
        return False

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_tool_result_event(
        self,
        action_step: ActionStep,
        result: str | None,
        *,
        error: str | None = None,
        result_file: str | None = None,
    ) -> None:
        spec = self._tool_registry.get_spec(action_step.tool_id)
        max_chars = spec.max_result_chars if spec else 2000
        # `summary` is a short preview for log titles / agent-tree rendering;
        # the full payload lives in `result`, which the frontend renders in a
        # scrollable container. Keep `summary` capped at the LLM-context size
        # so it stays human-skimmable.
        if max_chars and result and len(result) > max_chars:
            summary = error or result[:max_chars]
        else:
            summary = error or result or ""
        payload: dict[str, Any] = {
            "tool_id": action_step.tool_id,
            "operation": action_step.operation,
            "tool_input": action_step.tool_input,
            "result": result,
            "success": error is None,
            "summary": f"ERROR: {error}" if error else summary,
        }
        if error:
            payload["error"] = error
        if result_file:
            payload["result_file"] = result_file
        # Always tag with agent_id and model so the console can display
        # badges for all agents including the root.
        payload["agent_id"] = self._ctx.agent_id
        payload["model"] = self._ctx.model_name
        self._emit_event({"type": "tool_result", "payload": payload})

    def _emit_event(self, event: Event) -> None:
        if self._ctx.event_logger is not None:
            self._ctx.event_logger(event)

    @staticmethod
    def _extract_text_content(content: object) -> str:
        """Extract plain text from an AIMessage content field."""
        if isinstance(content, list):
            text = "\n".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ).strip()
        else:
            text = (str(content) if content else "").strip()
        # Drop the internal "(no content)" placeholder so it never surfaces
        # in ``agent_message`` events. Matches Claude Code's display filter
        # (src/utils/messages.ts:717).
        if text == _NO_CONTENT_PLACEHOLDER:
            return ""
        return text


# ------------------------------------------------------------------
# Standalone helpers
# ------------------------------------------------------------------


def _append_lsp_feedback(content: str, file_path: str, cwd: str) -> str:
    """Append passive LSP diagnostics to an edit tool result (if available)."""
    try:
        from mewbo_tools.integration.lsp import get_passive_diagnostics

        feedback = get_passive_diagnostics(file_path, cwd)
        if feedback:
            return f"{content}\n\n--- Passive Feedback (LSP) ---\n{feedback}"
    except Exception:
        pass  # LSP not installed or not running — silently skip
    return content


def _infer_operation(tool_id: str) -> str:
    """Map a tool_id to 'get' or 'set' for AbstractTool dispatch."""
    lowered = tool_id.lower()
    if any(keyword in lowered for keyword in _OPERATION_SET_KEYWORDS):
        return "set"
    if any(keyword in lowered for keyword in _OPERATION_GET_KEYWORDS):
        return "get"
    return "set"


def _coerce_mcp_tool_input(action_step: ActionStep, spec: ToolSpec) -> str | None:
    """Validate and coerce tool_input for MCP tools against their schema.

    Returns an error string if coercion fails, or None on success.
    Mutates ``action_step.tool_input`` in place when coercion is needed.
    """
    if spec.kind != "mcp":
        return None
    schema = spec.metadata.get("schema") if spec.metadata else None
    if not isinstance(schema, dict):
        return None
    required = schema.get("required") or []
    properties = schema.get("properties") or {}
    if not isinstance(properties, dict):
        properties = {}
    expected_fields = list(required) or list(properties.keys())

    argument = action_step.tool_input
    if isinstance(argument, str):
        stripped = argument.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                action_step.tool_input = parsed
                argument = parsed
        if isinstance(argument, str):
            if expected_fields:
                preferred_fields = ["query", "question", "input", "text", "q"]
                target_field = None
                if len(expected_fields) == 1:
                    target_field = expected_fields[0]
                else:
                    for preferred in preferred_fields:
                        if preferred in expected_fields:
                            target_field = preferred
                            break
                if target_field:
                    action_step.tool_input = {target_field: argument}
                    return None
            fields = ", ".join(expected_fields) if expected_fields else "schema-defined fields"
            return f"Expected JSON object with fields: {fields}."

    if isinstance(argument, dict):
        if required:
            missing = [name for name in required if name not in argument]
            if missing:
                if len(required) == 1 and len(argument) == 1:
                    required_field = required[0]
                    value = next(iter(argument.values()))
                    prop = properties.get(required_field, {})
                    if (
                        isinstance(prop, dict)
                        and prop.get("type") == "array"
                        and isinstance(value, str)
                    ):
                        items = prop.get("items")
                        if isinstance(items, dict) and items.get("type") == "string":
                            value = [value]
                    if (
                        isinstance(prop, dict)
                        and prop.get("type") == "string"
                        and isinstance(value, list)
                        and len(value) == 1
                    ):
                        value = value[0]
                    action_step.tool_input = {required_field: value}
                    return None
                return f"Missing required fields: {', '.join(missing)}."
        return None

    return "Unsupported tool_input type for MCP tool."


__all__ = ["ToolCallResult", "ToolUseLoop"]
