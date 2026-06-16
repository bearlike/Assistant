"""Golden byte-equality tests for the migrated ``planning.*`` / ``catalog.*`` prompts.

Phase 1 of the central prompt registry (Gitea #89) is a VERBATIM extraction: the
hardcoded section wrappers in ``planning.py:PromptBuilder.build`` and the catalog
header/line f-strings in ``skills.py`` / ``agent_registry.py`` / ``hypervisor.py``
move into ``prompts/registry/{assembly,catalog}.yaml`` with ZERO behaviour change.
Each ``EXPECTED`` below is the ORIGINAL literal copied verbatim; the test asserts
``render(...)`` reproduces it byte-for-byte for representative inputs. If a render
drifts by even one character (a stripped newline, a re-flowed line), the golden
fails — which is the whole point.
"""

from __future__ import annotations

from mewbo_core.prompt_registry import get_prompt_registry

# ---------------------------------------------------------------------------
# Verbatim copies of the ORIGINAL literals (the migration's source of truth).
# ---------------------------------------------------------------------------


# planning.py:PromptBuilder.build — section wrappers (the f-strings / concats).
def _expected_project_instructions(project_instructions: str) -> str:
    return f"Project instructions:\n{project_instructions}"


def _expected_session_summary(summary: str) -> str:
    return f"Session summary:\n{summary}"


def _expected_relevant_earlier_context(rendered: str) -> str:
    return "Relevant earlier context:\n" + rendered


def _expected_recent_conversation(rendered: str) -> str:
    return "Recent conversation:\n" + rendered


def _expected_available_tools(tool_lines: str) -> str:
    return f"Available tools:\n{tool_lines}"


def _expected_tool_guidance(tool_prompts_joined: str) -> str:
    return "Tool guidance:\n" + tool_prompts_joined


def _expected_component_status(status: str) -> str:
    return "Component status:\n" + status


# skills.py:SkillRegistry.render_catalog — header + per-line.
EXPECTED_SKILLS_HEADER = (
    "Available skills (use activate_skill to load instructions when relevant):"
)


def _expected_skill_line(name: str, description: str) -> str:
    return f"- {name}: {description}"


# agent_registry.py:AgentRegistry.render_catalog — header + per-line.
EXPECTED_AGENT_TYPES_HEADER = (
    "Available agent types (use spawn_agent with agent_type to delegate):"
)


def _expected_agent_type_line(name: str, description: str) -> str:
    return f"- {name}: {description}"


# hypervisor.py:render_agent_tree — the per-line / fragment f-strings.
def _expected_tree_header(status_parts: str, budget_str: str) -> str:
    return f"Agents: {status_parts}{budget_str}"


def _expected_tree_budget(total_steps: int, session_step_budget: int) -> str:
    return f" | Budget: {total_steps}/{session_step_budget} steps"


def _expected_tree_step_info_last(last_tool_id: str) -> str:
    return f", last: {last_tool_id}"


def _expected_tree_result(status: str, summary: str) -> str:
    return f" | result({status}): {summary[:120]}"


def _expected_tree_progress(progress_note: str) -> str:
    return f" | progress: {progress_note[:120]}"


def _expected_tree_compact(compaction_count: int) -> str:
    return f" | compacted x{compaction_count}"


def _expected_tree_line(
    indent: str,
    agent_id: str,
    status: str,
    task_preview: str,
    step_info: str,
    status_marker: str,
    compact_marker: str,
    extra: str,
) -> str:
    return (
        f"{indent}- [{agent_id[:8]}] {status}: "
        f'"{task_preview}" ({step_info}{status_marker}{compact_marker}{extra})'
    )


# ---------------------------------------------------------------------------
# planning.* section wrappers — byte-equality across representative inputs.
# ---------------------------------------------------------------------------


def test_section_project_instructions_matches_fstring():
    reg = get_prompt_registry()
    for value in ["Be concise.", "Line one\nLine two"]:
        assert reg.render(
            "planning.section.project_instructions", project_instructions=value
        ) == _expected_project_instructions(value)


def test_section_session_summary_matches_fstring():
    reg = get_prompt_registry()
    for value in ["A short recap.", "multi\nline\nsummary"]:
        assert reg.render(
            "planning.section.session_summary", summary=value
        ) == _expected_session_summary(value)


def test_section_relevant_earlier_context_matches_concat():
    reg = get_prompt_registry()
    for rendered in ["- user: hi", "line a\nline b"]:
        assert reg.render(
            "planning.section.relevant_earlier_context", rendered=rendered
        ) == _expected_relevant_earlier_context(rendered)


def test_section_recent_conversation_matches_concat():
    reg = get_prompt_registry()
    for rendered in ["- user: status?", "a\nb\nc"]:
        assert reg.render(
            "planning.section.recent_conversation", rendered=rendered
        ) == _expected_recent_conversation(rendered)


def test_section_available_tools_matches_fstring():
    reg = get_prompt_registry()
    for tool_lines in ["- shell: run", "- a: x\n- b: y"]:
        assert reg.render(
            "planning.section.available_tools", tool_lines=tool_lines
        ) == _expected_available_tools(tool_lines)


def test_section_tool_guidance_matches_concat():
    reg = get_prompt_registry()
    for joined in ["guidance one", "first\n\nsecond"]:
        assert reg.render(
            "planning.section.tool_guidance", tool_prompts_joined=joined
        ) == _expected_tool_guidance(joined)


def test_section_component_status_matches_concat():
    reg = get_prompt_registry()
    for status in ["Langfuse: ok", "a: 1\nb: 2"]:
        assert reg.render(
            "planning.section.component_status", status=status
        ) == _expected_component_status(status)


# ---------------------------------------------------------------------------
# catalog.skills / catalog.agent_types — header (static) + joined-line var.
# ---------------------------------------------------------------------------


def test_skills_catalog_header_and_lines_verbatim():
    reg = get_prompt_registry()
    auto = [("alpha", "first skill"), ("beta", "second skill")]
    lines = [EXPECTED_SKILLS_HEADER]
    for name, desc in auto:
        lines.append(_expected_skill_line(name, desc))
    expected = "\n".join(lines)
    skill_lines = "\n".join(_expected_skill_line(n, d) for n, d in auto)
    assert reg.render("catalog.skills", skill_lines=skill_lines) == expected
    # Header alone is recoverable for the no-skill caller branch.
    assert reg.render("catalog.skills.header") == EXPECTED_SKILLS_HEADER


def test_agent_types_catalog_header_and_lines_verbatim():
    reg = get_prompt_registry()
    visible = [("explorer", "read-only search"), ("planner", "designs plans")]
    lines = [EXPECTED_AGENT_TYPES_HEADER]
    for name, desc in visible:
        lines.append(_expected_agent_type_line(name, desc))
    expected = "\n".join(lines)
    agent_lines = "\n".join(_expected_agent_type_line(n, d) for n, d in visible)
    assert reg.render("catalog.agent_types", agent_lines=agent_lines) == expected
    assert reg.render("catalog.agent_types.header") == EXPECTED_AGENT_TYPES_HEADER


# ---------------------------------------------------------------------------
# catalog.agent_tree.* — per-line / fragment formats.
# ---------------------------------------------------------------------------


def test_agent_tree_header_matches_fstring():
    reg = get_prompt_registry()
    for status_parts, budget_str in [
        ("1 running", ""),
        ("1 running, 2 completed", " | Budget: 5/40 steps"),
    ]:
        assert reg.render(
            "catalog.agent_tree.header",
            status_parts=status_parts,
            budget_str=budget_str,
        ) == _expected_tree_header(status_parts, budget_str)


def test_agent_tree_budget_matches_fstring():
    reg = get_prompt_registry()
    for total, budget in [(5, 40), (0, 100)]:
        assert reg.render(
            "catalog.agent_tree.budget",
            total_steps=total,
            session_step_budget=budget,
        ) == _expected_tree_budget(total, budget)


def test_agent_tree_step_info_last_matches_fstring():
    reg = get_prompt_registry()
    for last in ["shell", "internet_search"]:
        assert reg.render(
            "catalog.agent_tree.step_info_last", last_tool_id=last
        ) == _expected_tree_step_info_last(last)


def test_agent_tree_result_matches_fstring_including_120_truncation():
    reg = get_prompt_registry()
    # The [:120] slice stays in code (the call site), so pass the pre-sliced value.
    for status, summary in [
        ("completed", "all good"),
        ("failed", "x" * 200),
    ]:
        assert reg.render(
            "catalog.agent_tree.result", status=status, summary=summary[:120]
        ) == _expected_tree_result(status, summary)


def test_agent_tree_progress_matches_fstring_including_120_truncation():
    reg = get_prompt_registry()
    # The [:120] slice stays in code (the call site), so pass the pre-sliced value.
    for note in ["working on it", "y" * 200]:
        assert reg.render(
            "catalog.agent_tree.progress", progress_note=note[:120]
        ) == _expected_tree_progress(note)


def test_agent_tree_compact_matches_fstring():
    reg = get_prompt_registry()
    for count in [1, 3]:
        assert reg.render(
            "catalog.agent_tree.compact", compaction_count=count
        ) == _expected_tree_compact(count)


def test_agent_tree_line_matches_fstring():
    reg = get_prompt_registry()
    cases = [
        ("", "abcdef1234", "running", "do the thing", "3 steps", "", "", ""),
        (
            "  ",
            "deadbeef99",
            "completed",
            "explore the repo",
            "5 steps, last: shell",
            " -> success",
            " | compacted x2",
            " | result(completed): done",
        ),
    ]
    for (
        indent,
        agent_id,
        status,
        task_preview,
        step_info,
        status_marker,
        compact_marker,
        extra,
    ) in cases:
        assert reg.render(
            "catalog.agent_tree.line",
            indent=indent,
            agent_id_head=agent_id[:8],
            status=status,
            task_preview=task_preview,
            step_info=step_info,
            status_marker=status_marker,
            compact_marker=compact_marker,
            extra=extra,
        ) == _expected_tree_line(
            indent,
            agent_id,
            status,
            task_preview,
            step_info,
            status_marker,
            compact_marker,
            extra,
        )


# ---------------------------------------------------------------------------
# The whole assembly.yaml / catalog.yaml is well-formed (declared == used).
# ---------------------------------------------------------------------------


def test_assembly_and_catalog_entries_validate():
    reg = get_prompt_registry()
    reg.validate_all()
    for pid in [
        "planning.section.project_instructions",
        "planning.section.session_summary",
        "planning.section.relevant_earlier_context",
        "planning.section.recent_conversation",
        "planning.section.available_tools",
        "planning.section.tool_guidance",
        "planning.section.component_status",
        "catalog.skills",
        "catalog.skills.header",
        "catalog.agent_types",
        "catalog.agent_types.header",
        "catalog.agent_tree.header",
        "catalog.agent_tree.budget",
        "catalog.agent_tree.step_info_last",
        "catalog.agent_tree.result",
        "catalog.agent_tree.progress",
        "catalog.agent_tree.compact",
        "catalog.agent_tree.line",
    ]:
        assert reg.has(pid)
