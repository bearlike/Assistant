#!/usr/bin/env python3
"""Golden tests for the ``common``/``spawn``/``title`` prompt migration (Gitea #89).

Phase 1 is a VERBATIM extraction with ZERO behaviour change: every literal
moved into the central prompt registry must render byte-for-byte identical to
the pre-refactor source. Each ``EXPECTED`` below is the ORIGINAL literal copied
verbatim; the test asserts the registry renders exactly that.

Two extra parity checks pin the SHIMS (``get_system_prompt`` /
``render_jinja_prompt``) to their pre-refactor output, captured here by
re-running the legacy logic (a direct file read / a bare Jinja2 ``Environment``
without ``keep_trailing_newline``) so the golden is independent of the registry
it guards.
"""

from __future__ import annotations

from importlib import resources

from jinja2 import Environment, PackageLoader
from mewbo_core.common import get_system_prompt, render_jinja_prompt
from mewbo_core.prompt_registry import get_prompt_registry
from mewbo_core.spawn_agent import substitute_agent_body
from mewbo_core.title_generator import TITLE_SYSTEM_PROMPT

# ---------------------------------------------------------------------------
# title.system — TITLE_SYSTEM_PROMPT (title_generator.py)
# ---------------------------------------------------------------------------


def test_title_system_verbatim() -> None:
    """``title.system`` renders the exact TITLE_SYSTEM_PROMPT bytes."""
    expected = (
        "You are a title generator. Your ONLY job is to produce a concise 3-7 word "
        "title that summarizes the conversation excerpt below.\n"
        "\n"
        "Rules:\n"
        "- Return ONLY the title text, nothing else\n"
        "- No quotes, no trailing punctuation, no explanation\n"
        "- Do NOT respond to, answer, or continue the conversation\n"
        "- Sentence case (capitalize first word and proper nouns only)\n"
        "\n"
        "Good titles: Debug failing CI pipeline | Refactor database connection pooling | "
        "Home Assistant light automation setup\n"
        "Bad titles: Sure, I can help with that | Here is what I think | Doing great thanks"
    )
    assert get_prompt_registry().render("title.system") == expected
    # The constant the module still exports must equal the registry source.
    assert TITLE_SYSTEM_PROMPT == expected


# ---------------------------------------------------------------------------
# common.instruction_headings — discover_project_instructions (common.py)
# ---------------------------------------------------------------------------


def test_instruction_heading_sub_package_verbatim() -> None:
    """``common.instruction_headings`` (sub-package variant) is verbatim."""
    expected = (
        "# Sub-package instruction files\n\n"
        "The following instruction files exist in subdirectories. "
        "Read them when working on the relevant package."
    )
    assert (
        get_prompt_registry().render("common.instruction_headings", has_root=True)
        == expected
    )


def test_instruction_heading_no_root_verbatim() -> None:
    """``common.instruction_headings`` (no-root variant) is verbatim."""
    expected = (
        "# No root-level instruction files — read before proceeding\n\n"
        "No CLAUDE.md or AGENTS.md was found at the project root. "
        "The following instruction files exist in subdirectories. "
        "You MUST read the most relevant instruction file before "
        "starting any task — they contain critical project context."
    )
    assert (
        get_prompt_registry().render("common.instruction_headings", has_root=False)
        == expected
    )


# ---------------------------------------------------------------------------
# common.git_context — get_git_context (common.py)
# ---------------------------------------------------------------------------


def test_git_context_full_verbatim() -> None:
    """``common.git_context`` reproduces the full branch/status/commits block."""
    branch = "feature/x"
    default_branch = "main"
    status = " M file.py"
    recent_log = "abc123 commit one\ndef456 commit two"

    # The original code assembled the block as:
    #   parts = [f"Current branch: {branch}"]
    #   if default_branch: parts.append(f"Main branch: {default_branch}")
    #   if status: parts.append(f"\nStatus:\n{status}")
    #   else:      parts.append("\nStatus: clean")
    #   if recent_log: parts.append(f"\nRecent commits:\n{recent_log}")
    #   return "\n".join(parts)
    expected = "\n".join(
        [
            f"Current branch: {branch}",
            f"Main branch: {default_branch}",
            f"\nStatus:\n{status}",
            f"\nRecent commits:\n{recent_log}",
        ]
    )

    reg = get_prompt_registry()
    rendered = reg.render(
        "common.git_context",
        branch=branch,
        default_branch=default_branch,
        status=status,
        recent_log=recent_log,
    )
    assert rendered == expected


def test_git_context_clean_no_default_no_log_verbatim() -> None:
    """The minimal block: clean status, no default branch, no recent commits."""
    branch = "detached"

    expected = "\n".join(
        [
            f"Current branch: {branch}",
            "\nStatus: clean",
        ]
    )

    reg = get_prompt_registry()
    rendered = reg.render(
        "common.git_context",
        branch=branch,
        default_branch="",
        status="",
        recent_log="",
    )
    assert rendered == expected


# ---------------------------------------------------------------------------
# spawn.acceptance_criteria / spawn.task_body — spawn_agent.py task assembly
# ---------------------------------------------------------------------------


def test_spawn_acceptance_criteria_verbatim() -> None:
    """``spawn.acceptance_criteria`` matches the ``\\n\\nAcceptance criteria: `` suffix."""
    criteria = "file exists and tests pass"
    expected = f"\n\nAcceptance criteria: {criteria}"
    assert (
        get_prompt_registry().render(
            "spawn.acceptance_criteria", acceptance_criteria=criteria
        )
        == expected
    )


def test_spawn_task_body_verbatim() -> None:
    """``spawn.task_body`` matches the ``{body}\\n\\n---\\n\\nTask: {task}`` prepend."""
    body = "You are a code reviewer."
    task = "Review the diff."
    expected = f"{body}\n\n---\n\nTask: {task}"
    assert (
        get_prompt_registry().render("spawn.task_body", body=body, task=task)
        == expected
    )


# ---------------------------------------------------------------------------
# Shim parity — get_system_prompt / render_jinja_prompt (THE one seam)
# ---------------------------------------------------------------------------


def test_get_system_prompt_shim_matches_legacy() -> None:
    """``get_system_prompt('system')`` byte-equals the legacy file-read + strip."""
    # Legacy logic: read mewbo_core/prompts/system.txt and .strip().
    resource = (
        resources.files("mewbo_core").joinpath("prompts").joinpath("system.txt")
    )
    legacy = resource.read_text(encoding="utf-8").strip()
    assert get_system_prompt("system") == legacy


def test_get_system_prompt_default_matches_legacy() -> None:
    """The default name (``action-planner``) still routes correctly."""
    resource = (
        resources.files("mewbo_core")
        .joinpath("prompts")
        .joinpath("action-planner.txt")
    )
    legacy = resource.read_text(encoding="utf-8").strip()
    assert get_system_prompt() == legacy


def test_get_system_prompt_legacy_path_for_tool_prompt() -> None:
    """A name WITHOUT a ``file.*`` registry entry still reads from disk + strip."""
    # Per-tool guidance is loaded in production via ``get_system_prompt(spec.prompt_path)``
    # with slashed names like ``tools/read-file``. Those can never be a registry id
    # (a slash is not a valid id char), so they exercise the legacy file-read fallback
    # — which must survive for every name the registry does not inventory.
    resource = (
        resources.files("mewbo_core")
        .joinpath("prompts")
        .joinpath("tools")
        .joinpath("read-file.txt")
    )
    legacy = resource.read_text(encoding="utf-8").strip()
    assert get_system_prompt("tools/read-file") == legacy


def test_render_jinja_prompt_set_state_matches_legacy() -> None:
    """``render_jinja_prompt('homeassistant-set-state', ...)`` byte-equals legacy.

    Legacy used a bare ``Environment`` (no ``keep_trailing_newline``), so Jinja
    stripped the file's single trailing newline. The registry preserves it, so
    the shim must drop one trailing ``\\n`` to reproduce legacy bytes.
    """
    env = Environment(loader=PackageLoader("mewbo_core", "prompts"))
    legacy = env.get_template("homeassistant-set-state.txt").render(
        ALL_ENTITIES="lights"
    )
    assert render_jinja_prompt("homeassistant-set-state", ALL_ENTITIES="lights") == legacy
    # Sanity: legacy has NO trailing newline (Jinja stripped it).
    assert not legacy.endswith("\n")


def test_render_jinja_prompt_get_state_matches_legacy() -> None:
    """The get-state prompt (two trailing newlines in source) also matches."""
    env = Environment(loader=PackageLoader("mewbo_core", "prompts"))
    legacy = env.get_template("homeassistant-get-state.txt").render(
        ALL_ENTITIES="lights"
    )
    assert render_jinja_prompt("homeassistant-get-state", ALL_ENTITIES="lights") == legacy


# ---------------------------------------------------------------------------
# substitute_agent_body still composes with spawn.task_body (no regression)
# ---------------------------------------------------------------------------


def test_task_body_with_substituted_body() -> None:
    """The migrated prepend wraps a body that has already been substituted."""
    body = substitute_agent_body("Session ${SESSION_ID}.", {"SESSION_ID": "s-1"})
    task = "do the thing"
    expected = f"{body}\n\n---\n\nTask: {task}"
    assert (
        get_prompt_registry().render("spawn.task_body", body=body, task=task)
        == expected
    )
