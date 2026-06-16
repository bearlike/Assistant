"""Golden byte-equality tests for the migrated tool-use-loop prompts (Gitea #89).

Phase 1 is a VERBATIM extraction: the registry must reproduce the exact bytes
that ``tool_use_loop.py`` previously hardcoded (system-prompt section wrappers,
the depth/delegation role prompts, budget/stall/interrupt injections, the
final-answer synthesis directive, the compaction drive/re-injection markers,
and the plan-mode reminders). Each ``EXPECTED_*`` literal below is a frozen copy
of the original constant/f-string; if a future edit retunes a prompt, the
byte-equality assertion fails loudly. The originals live in the registry now
(``loop.yaml``); this test is the contract that the migration changed nothing.

A representative ``model=`` is passed to the per-step injected prompts (section
wrappers + depth guidance) to prove they still render the base template — no
model override exists yet, so per-model convergence wiring is a no-op today.
"""

from __future__ import annotations

from mewbo_core.prompt_registry import get_prompt_registry

# A model that does not match any override prefix — proves base render holds.
_MODEL = "claude-opus-4-8"


# --- _build_messages section wrappers (frozen originals) ---------------------


def _expected_environment(work_dir: str, platform: str, date: str, version: str) -> str:
    return "\n".join(
        [
            "# Environment",
            f"- Working directory: {work_dir}",
            f"- Platform: {platform}",
            f"- Date: {date}",
            f"- Mewbo version: {version}",
        ]
    )


def test_section_environment_is_verbatim():
    reg = get_prompt_registry()
    expected = _expected_environment("/work", "linux", "2026-06-12", "1.2.3")
    assert (
        reg.render(
            "loop.section.environment",
            model=_MODEL,
            work_dir="/work",
            platform="linux",
            date="2026-06-12",
            version="1.2.3",
        )
        == expected
    )


def test_section_project_instructions_is_verbatim():
    reg = get_prompt_registry()
    project = "Use tabs. Never push."
    assert (
        reg.render("loop.section.project_instructions", model=_MODEL, project_instructions=project)
        == f"Project instructions:\n{project}"
    )


def test_section_agent_tree_is_verbatim():
    reg = get_prompt_registry()
    tree = "root\n └─ child"
    assert (
        reg.render("loop.section.agent_tree", model=_MODEL, agent_tree=tree)
        == f"# Active agent tree\n{tree}"
    )


def test_section_git_context_is_verbatim():
    reg = get_prompt_registry()
    git = "branch: main\nstatus: clean"
    assert (
        reg.render("loop.section.git_context", model=_MODEL, git_ctx=git)
        == f"# Git Context\n{git}"
    )


def test_section_skill_instructions_is_verbatim():
    reg = get_prompt_registry()
    skill = "Follow the smoketest steps."
    assert (
        reg.render("loop.section.skill_instructions", model=_MODEL, skill_instructions=skill)
        == f"Active skill instructions:\n{skill}"
    )


def test_section_session_summary_is_verbatim():
    reg = get_prompt_registry()
    summary = "Prior turns summarized."
    assert (
        reg.render("loop.section.session_summary", model=_MODEL, summary=summary)
        == f"Session summary:\n{summary}"
    )


def test_section_recent_conversation_is_verbatim():
    reg = get_prompt_registry()
    rendered = "[user] hi\n[assistant] hello"
    assert (
        reg.render("loop.section.recent_conversation", model=_MODEL, rendered=rendered)
        == f"Recent conversation:\n{rendered}"
    )


def test_section_attached_files_is_verbatim():
    reg = get_prompt_registry()
    joined = "file a\n---\nfile b"
    assert (
        reg.render("loop.section.attached_files", model=_MODEL, joined=joined)
        == "Attached files:\n" + joined
    )


def test_section_tool_guidance_is_verbatim():
    reg = get_prompt_registry()
    guidance = "Use read_file before edit_file."
    assert (
        reg.render("loop.section.tool_guidance", model=_MODEL, tool_guidance=guidance)
        == f"Tool guidance:\n{guidance}"
    )


def test_section_deferred_tools_is_verbatim():
    reg = get_prompt_registry()
    assert reg.render("loop.section.deferred_tools", model=_MODEL) == (
        "Schemas are not loaded — call `tool_search` with keywords "
        "(e.g. server name, action) or `select:<tool_id>` to load them "
        "before invoking."
    )


def test_section_plan_execution_is_verbatim():
    reg = get_prompt_registry()
    plan_lines = "1. First — do a\n2. Second — do b"
    assert reg.render("loop.section.plan_execution", model=_MODEL, plan_lines=plan_lines) == (
        f"Execute this plan:\n{plan_lines}\n"
        "Follow steps in order. Adapt if results require it."
    )


# --- _build_depth_guidance (frozen originals) --------------------------------


def _expected_root_act(depth: int, max_depth: int) -> str:
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
        "- Fanning out N independent subtasks? Prefer ONE spawn_agents(tasks=[...])"
        " call over N spawn_agent calls — reliable parallel admission in a"
        " single turn.",
    ]
    lines.extend(_shared_root_sections())
    return "\n".join(lines)


def _expected_root_plan(depth: int, max_depth: int) -> str:
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
    lines.extend(_shared_root_sections())
    return "\n".join(lines)


def _shared_root_sections() -> list[str]:
    return [
        "",
        "## Async delegation protocol (when you spawn)",
        "- spawn_agent returns immediately with {agent_id, status: 'submitted'}.",
        "- spawn_agents(tasks=[...]) fans out many at once, returning ordered"
        " agent_ids (per-slot 'rejected' if the pool is full) — the preferred"
        " path for independent subtasks.",
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


def _expected_leaf(depth: int, max_depth: int) -> str:
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
    return "\n".join(lines)


def _expected_suborch(depth: int, max_depth: int, remaining: int) -> str:
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
    return "\n".join(lines)


_EXPECTED_BOUNDARY = (
    "\nDELEGATION BOUNDARY: You are deep in the agent tree. "
    "Prefer direct tool use over spawning."
)


def test_depth_root_act_is_verbatim():
    reg = get_prompt_registry()
    assert (
        reg.render("loop.depth.root", model=_MODEL, plan_mode=False, depth=0, max_depth=3)
        == _expected_root_act(0, 3)
    )


def test_depth_root_plan_is_verbatim():
    reg = get_prompt_registry()
    assert (
        reg.render("loop.depth.root", model=_MODEL, plan_mode=True, depth=0, max_depth=3)
        == _expected_root_plan(0, 3)
    )


def test_depth_leaf_is_verbatim():
    reg = get_prompt_registry()
    assert (
        reg.render("loop.depth.leaf", model=_MODEL, depth=2, max_depth=3)
        == _expected_leaf(2, 3)
    )


def test_depth_suborchestrator_is_verbatim():
    reg = get_prompt_registry()
    assert (
        reg.render("loop.depth.suborchestrator", model=_MODEL, depth=1, max_depth=4, remaining=3)
        == _expected_suborch(1, 4, 3)
    )


def test_depth_boundary_is_verbatim():
    reg = get_prompt_registry()
    assert reg.render("loop.depth.boundary", model=_MODEL) == _EXPECTED_BOUNDARY


# --- Per-step injections / markers (frozen originals) ------------------------


def test_interrupt_marker_is_verbatim():
    reg = get_prompt_registry()
    assert reg.render("loop.interrupt_marker") == "[System: Current step interrupted by user.]"


def test_budget_warning_is_verbatim():
    reg = get_prompt_registry()
    assert reg.render("loop.budget_warning") == (
        "BUDGET WARNING: Session step budget nearly exhausted. "
        "Summarize your current findings and return results "
        "immediately."
    )


def test_agent_results_header_is_verbatim():
    reg = get_prompt_registry()
    result_lines = "[abcd1234] completed: did the thing"
    assert (
        reg.render("loop.agent_results_header", joined=result_lines)
        == "Completed sub-agent results:\n" + result_lines
    )


def test_agents_still_running_is_verbatim():
    reg = get_prompt_registry()
    count = 2
    ids = "abcd1234, ef567890"
    assert reg.render("loop.agents_still_running", count=count, ids=ids) == (
        f"WARNING: {count} agent(s) "
        f"still running ({ids}). Include their partial "
        "progress in your synthesis."
    )


def test_final_answer_synthesis_is_verbatim():
    reg = get_prompt_registry()
    assert reg.render("loop.final_answer_synthesis") == (
        "You MUST now provide your final answer based on "
        "all the information gathered so far. "
        "Do NOT call any more tools. Respond with text only."
    )


def test_stall_warning_is_verbatim():
    reg = get_prompt_registry()
    assert (
        reg.render("loop.stall_warning")
        == "STALL WARNING: No progress for 2+ minutes. Wrap up or report status."
    )


def test_compaction_drive_is_verbatim():
    reg = get_prompt_registry()
    summary_input = "[user] hi\n[assistant] hello"
    assert (
        reg.render("loop.compaction_drive", summary_input=summary_input)
        == f"Summarize this conversation:\n\n{summary_input}"
    )


def test_compacted_marker_is_verbatim():
    reg = get_prompt_registry()
    summary = "## Primary Request\nDo a thing."
    assert (
        reg.render("loop.compacted_marker", summary=summary)
        == f"[Compacted context]\n{summary}"
    )


# --- Plan-mode prompts (frozen originals) ------------------------------------


def test_plan_file_suffix_is_verbatim():
    reg = get_prompt_registry()
    plan_path = "/home/u/.mewbo/plans/sess.md"
    assert (
        reg.render("loop.plan_file_suffix", plan_path=plan_path)
        == f"\n\nPlan file: {plan_path}"
    )


_EXPECTED_PLAN_MODE_REMINDER = """\
# Plan Mode

You are the planning agent. Explore the codebase and design an implementation plan.

Use the read-only tools available to you to understand the code. Write your plan
into {plan_path} using your edit tool — the file already exists.

exit_plan_mode will reject if the plan file is empty. You must write to the file
with the edit tool before calling it. Do not output the plan as text — it must
be in the file.

Your plan should cover:
- Context: the problem this change addresses
- Approach: recommended implementation strategy with file paths
- Reuse: existing functions and patterns to leverage
- Verification: how to test the changes

Shell commands permitted: {shell_allowlist_bullets}
"""


def test_plan_mode_reminder_is_verbatim():
    reg = get_prompt_registry()
    plan_path = "/home/u/.mewbo/plans/sess.md"
    bullets = "    - `ls`\n    - `cat`"
    # The original call site read the file via get_system_prompt(), which
    # .strip()s — so the historical bytes carried NO trailing newline. The
    # registry entry uses `|-` to reproduce that exactly.
    expected = (
        _EXPECTED_PLAN_MODE_REMINDER.strip()
        .replace("{plan_path}", plan_path)
        .replace("{shell_allowlist_bullets}", bullets)
    )
    assert (
        reg.render(
            "loop.plan_mode_reminder",
            plan_path=plan_path,
            shell_allowlist_bullets=bullets,
        )
        == expected
    )


def test_plan_edit_restricted_is_verbatim():
    reg = get_prompt_registry()
    plan_path = "/home/u/.mewbo/plans/sess.md"
    attempted = "/etc/passwd"
    assert reg.render("loop.plan_edit_restricted", plan_path=plan_path, attempted=attempted) == (
        f"Plan mode: edits restricted to {plan_path}. You attempted "
        f"to write {attempted}. Write only to the plan file, then "
        "call exit_plan_mode."
    )


def test_loop_registry_entries_validate():
    # Definition-time gate: templates parse, declared vars match, no drift.
    get_prompt_registry().validate_all()
