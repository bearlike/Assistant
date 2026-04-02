#!/usr/bin/env python3
"""Async tool-use conversation loop with sub-agent support."""

from __future__ import annotations

import asyncio
import json
import platform as _platform
import queue as _queue_mod
import re
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

from meeseeks_core.agent_context import AgentContext
from meeseeks_core.classes import ActionStep, OrchestrationState, Plan, TaskQueue
from meeseeks_core.common import get_git_context, get_logger, get_mock_speaker, get_system_prompt
from meeseeks_core.components import build_langfuse_handler, langfuse_trace_span
from meeseeks_core.config import get_config_value, get_version
from meeseeks_core.context import ContextSnapshot, render_event_lines
from meeseeks_core.hooks import HookManager
from meeseeks_core.hypervisor import AgentHandle
from meeseeks_core.llm import build_chat_model, specs_to_langchain_tools
from meeseeks_core.permissions import PermissionDecision, PermissionPolicy
from meeseeks_core.tool_registry import ToolRegistry, ToolSpec
from meeseeks_core.types import Event

logging = get_logger(name="core.tool_use_loop")

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

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
        cwd: str | None = None,
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
            cwd: Working directory for this agent (project root).
        """
        self._ctx = agent_context
        self._tool_registry = tool_registry
        self._permission_policy = permission_policy
        self._approval_callback = approval_callback
        self._hook_manager = hook_manager
        self._project_instructions = project_instructions
        self._skill_instructions = skill_instructions
        self._skill_registry = skill_registry
        self._cwd = cwd

        # Create SpawnAgentTool when this agent can spawn children.
        self._spawn_agent_tool: Any = None
        if agent_context.can_spawn:
            from meeseeks_core.spawn_agent import SpawnAgentTool

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
        max_steps: int = 10,
        plan: Plan | None = None,
    ) -> tuple[TaskQueue, OrchestrationState]:
        """Run the async tool-use loop and return TaskQueue + OrchestrationState."""
        state = OrchestrationState(goal=user_query)
        executed_steps: list[ActionStep] = []
        tool_outputs: list[str] = []
        last_error: str | None = None
        final_response: str | None = None

        # Register self in the hypervisor registry.
        handle = AgentHandle(
            agent_id=self._ctx.agent_id,
            parent_id=self._ctx.parent_id,
            depth=self._ctx.depth,
            model_name=self._ctx.model_name,
            task_description=user_query[:200],
        )
        await self._ctx.registry.register(handle)

        try:
            # Ref: [DeepMind-Delegation §4.5] Global eye for root agent
            agent_tree = ""
            if self._ctx.depth == 0:
                agent_tree = await self._ctx.registry.render_agent_tree()
            messages = self._build_messages(
                user_query,
                context,
                plan,
                agent_tree=agent_tree,
            )
            tool_schemas = specs_to_langchain_tools(tool_specs)
            model = self._bind_model(tool_schemas)

            langfuse_handler = build_langfuse_handler(
                user_id="meeseeks-tool-use",
                session_id=f"tool-use-{self._ctx.agent_id}",
                trace_name="meeseeks-tool-use",
                version=get_version(),
                release=get_config_value("runtime", "envmode", default="Not Specified"),
            )
            invoke_config: dict[str, Any] = {}
            if langfuse_handler is not None:
                invoke_config["callbacks"] = [langfuse_handler]

            steps_run = 0
            for _ in range(max_steps):
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

                with langfuse_trace_span("tool-use-step") as span:
                    if span is not None:
                        try:
                            span.update_trace(
                                input={"step": steps_run, "message_count": len(messages)}
                            )
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

                    response: AIMessage = await model.ainvoke(
                        messages, config=invoke_config or None
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
                        response = AIMessage(
                            content=sanitized or "",
                            tool_calls=response.tool_calls,
                            additional_kwargs=response.additional_kwargs,
                            id=response.id,
                        )
                    messages.append(response)

                    if not response.tool_calls:
                        # Text response — task is done.
                        raw_content = getattr(response, "content", "") or ""
                        if isinstance(raw_content, list):
                            # Claude extended thinking: extract text blocks only.
                            content = "\n".join(
                                block.get("text", "")
                                for block in raw_content
                                if isinstance(block, dict) and block.get("type") == "text"
                            )
                        else:
                            content = str(raw_content)
                        final_response = content
                        tool_outputs.append(content)
                        state.done = True
                        state.done_reason = "completed"
                        break

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

                    # Update registry step count.
                    await self._ctx.registry.update_step(
                        self._ctx.agent_id,
                        results[-1].tool_id if results else "",
                    )
                    steps_run += 1

                    # Inject step progress so the model can self-regulate.
                    remaining_steps = max_steps - steps_run
                    failures = [r for r in results if not r.success]
                    progress_parts = [
                        f"[Step {steps_run}/{max_steps}"
                        f" — {remaining_steps} step{'s' if remaining_steps != 1 else ''}"
                        " remaining]",
                    ]
                    if failures:
                        progress_parts.append(
                            f"{len(failures)}/{len(results)} tool call(s)"
                            " failed this step — adapt your approach."
                        )
                    if remaining_steps <= 2 and remaining_steps > 0:
                        progress_parts.append(
                            "Approaching step limit: wrap up and synthesize your findings soon."
                        )
                    messages.append(SystemMessage(content=" ".join(progress_parts)))

                    if span is not None:
                        try:
                            span.update_trace(
                                output={
                                    "tool_calls": len(response.tool_calls),
                                    "steps_run": steps_run,
                                }
                            )
                        except Exception:
                            pass

            # When the step budget is exhausted without a text response,
            # give the LLM one final turn WITHOUT tools to force synthesis.
            if not state.done and final_response is None and messages:
                try:
                    messages.append(
                        SystemMessage(
                            content=(
                                "STEP LIMIT REACHED. You MUST now provide your final "
                                "answer based on all the information gathered so far. "
                                "Do NOT call any more tools. Respond with text only."
                            )
                        )
                    )
                    # Invoke without tool bindings to prevent further tool calls.
                    unbound = build_chat_model(
                        model_name=self._ctx.model_name,
                        openai_api_base=get_config_value("llm", "api_base"),
                        api_key=get_config_value("llm", "api_key"),
                    )
                    synthesis: AIMessage = await unbound.ainvoke(
                        messages, config=invoke_config or None
                    )
                    raw = getattr(synthesis, "content", "") or ""
                    if isinstance(raw, list):
                        final_response = "\n".join(
                            block.get("text", "")
                            for block in raw
                            if isinstance(block, dict) and block.get("type") == "text"
                        )
                    else:
                        final_response = str(raw)
                except Exception:
                    logging.warning("Final synthesis turn failed.", exc_info=True)

                # Fallback: if synthesis produced nothing, build a minimal
                # response from successful tool outputs so the user isn't
                # left with an empty result.
                if not final_response or not final_response.strip():
                    successful = [o for o in tool_outputs if o and not o.startswith("ERROR")]
                    if successful:
                        final_response = (
                            "Step limit reached before full synthesis. "
                            "Partial results:\n\n" + "\n\n".join(successful[-5:])
                        )

            if not state.done:
                state.done = True
                state.done_reason = "max_steps_reached" if steps_run >= max_steps else "completed"

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
            f"- Meeseeks version: {get_version()}",
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
            catalog = self._skill_registry.render_catalog()
            if catalog:
                system_parts.append(catalog)

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

        system_prompt = "\n\n".join(p for p in system_parts if p)
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
            # Ref: [CoA §3.2] Root = manager agent. Direct execution first.
            lines = [
                f"# Agent role: Root orchestrator (depth {depth}/{max_depth})",
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
                "- Scope sub-agents with allowed_tools/denied_tools and max_steps.",
                "",
                "## System awareness",
                "- You operate within a bounded environment with intentional guardrails.",
                "- CWD restrictions, permission denials, and tool scope limits are non-negotiable.",
                "- If a tool or sub-agent reports a restriction, do NOT retry with a different",
                "  agent or workaround. Adapt your approach or report the limitation.",
                "",
                "## When to stop",
                "- If the same operation fails twice, do not retry it a third time.",
                "- If a sub-agent fails, do not spawn another sub-agent for the same task.",
                "- Report what failed, why, and what you tried — then let the user decide.",
                "",
                "## Sub-agent results",
                "- Results are JSON with status/content/summary/steps_used fields.",
                "- Check 'status' before using: completed=reliable, failed/cannot_solve=handle.",
            ]
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
        from meeseeks_core.skills import activate_skill

        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        skill_name = str(args.get("skill_name", ""))
        skill_args = str(args.get("args", ""))

        registry = self._skill_registry
        skill = registry.get(skill_name) if registry else None
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

    def _bind_model(self, tool_schemas: list[dict[str, Any]]) -> Any:
        """Build a chat model and bind tool schemas."""
        model = build_chat_model(
            model_name=self._ctx.model_name,
            openai_api_base=get_config_value("llm", "api_base"),
            api_key=get_config_value("llm", "api_key"),
        )
        # Inject spawn_agent schema if this agent can spawn children.
        # (Wired in Phase 2 when self._spawn_agent_tool is set.)
        if self._spawn_agent_tool is not None:
            from meeseeks_core.spawn_agent import SPAWN_AGENT_SCHEMA

            tool_schemas = [*tool_schemas, SPAWN_AGENT_SCHEMA]
        # Inject activate_skill schema when auto-invocable skills exist.
        if self._skill_registry is not None and self._skill_registry.list_auto_invocable():
            from meeseeks_core.skills import ACTIVATE_SKILL_SCHEMA

            tool_schemas = [*tool_schemas, ACTIVATE_SKILL_SCHEMA]
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
            self._emit_tool_result_event(action_step, None, error="Permission denied")
            return ToolCallResult(
                tool_call_id=tool_call_id,
                tool_id=tool_id,
                content="Permission denied",
                success=False,
            )

        # Pre-tool hook.
        action_step = self._hook_manager.run_pre_tool_use(action_step)

        # Execute — internal tools (spawn_agent, activate_skill) first, then registry.
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
        if isinstance(content, dict):
            content_str = json.dumps(content, ensure_ascii=False, default=str)

        # Micro-compaction: strip ANSI escapes and truncate for the LLM.
        content_str = _ANSI_ESCAPE_RE.sub("", content_str)
        spec = self._tool_registry.get_spec(tool_id)
        max_chars = spec.max_result_chars if spec else 2000
        if max_chars and len(content_str) > max_chars:
            content_str = content_str[:max_chars] + "\n[truncated]"

        self._emit_tool_result_event(action_step, content_str)
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
        operation = _infer_operation(tool_id)
        return ActionStep(
            title=tool_id,
            tool_id=tool_id,
            operation=operation,
            tool_input=args,
        )

    # ------------------------------------------------------------------
    # Permission
    # ------------------------------------------------------------------

    def _check_permission(self, action_step: ActionStep) -> bool:
        """Check permission for an action step. Returns True if allowed."""
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

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_tool_result_event(
        self,
        action_step: ActionStep,
        result: str | None,
        *,
        error: str | None = None,
    ) -> None:
        spec = self._tool_registry.get_spec(action_step.tool_id)
        max_chars = spec.max_result_chars if spec else 2000
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
        # Tag with agent_id for sub-agent tracking.
        if self._ctx.depth > 0:
            payload["agent_id"] = self._ctx.agent_id
        self._emit_event({"type": "tool_result", "payload": payload})

    def _emit_event(self, event: Event) -> None:
        if self._ctx.event_logger is not None:
            self._ctx.event_logger(event)


# ------------------------------------------------------------------
# Standalone helpers
# ------------------------------------------------------------------


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
