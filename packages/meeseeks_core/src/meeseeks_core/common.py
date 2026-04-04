#!/usr/bin/env python3
"""Common helpers shared across the assistant runtime."""

from __future__ import annotations

import json
import logging as logging_real
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import NamedTuple

import tiktoken
from jinja2 import Environment, PackageLoader
from loguru import logger as loguru_logger

from meeseeks_core.config import get_config_value


class MockSpeaker(NamedTuple):
    """Simple mock response container used across tools and tests."""

    content: str


def get_mock_speaker() -> type[MockSpeaker]:
    """Return a mock speaker for testing."""
    return MockSpeaker


_LOG_CONFIGURED = False
_SESSION_SINKS: dict[str, dict[str, int]] = {}


def _resolve_log_level() -> str:
    level_name = get_config_value("runtime", "log_level", default="DEBUG")
    if isinstance(level_name, str) and level_name.strip():
        return level_name.strip().upper()
    return "DEBUG"


def _should_use_cli_dark_logs() -> bool:
    style = get_config_value("runtime", "log_style", default="")
    if not style:
        style = get_config_value("runtime", "cli_log_style", default="")
    return style.lower() == "dark"


def _configure_logging() -> None:
    global _LOG_CONFIGURED
    if _LOG_CONFIGURED:
        return
    log_level = _resolve_log_level()
    loggers_to_suppress = [
        "request",
        "httpcore",
        "urllib3.connectionpool",
        "openai._base_client",
        "aiohttp_client_cache.signatures",
        "LangChainDeprecationWarning",
        "watchdog.observers.inotify_buffer",
        "PIL.PngImagePlugin",
    ]
    for logger_name in loggers_to_suppress:
        logging_real.getLogger(logger_name).setLevel(logging_real.ERROR)

    loguru_logger.remove()
    colorize = sys.stderr.isatty()
    if _should_use_cli_dark_logs():
        format_str = (
            "<dim>{time:YYYY-MM-DD HH:mm:ss} [{extra[name]}] "
            "<level>{level}</level> {message}{exception}</dim>"
        )
    else:
        format_str = "{time:YYYY-MM-DD HH:mm:ss} [{extra[name]}] <level>{level}</level> {message}"
    loguru_logger.add(sys.stderr, level=log_level, format=format_str, colorize=colorize)
    _LOG_CONFIGURED = True


def _resolve_session_log_dir() -> str:
    cache_dir = get_config_value("runtime", "cache_dir", default=".cache")
    cache_dir = str(cache_dir or ".cache")
    return os.path.join(cache_dir, "session-logs")


def _session_log_format() -> str:
    return "{time:YYYY-MM-DD HH:mm:ss} [{extra[name]}] {level} {message}"


def _ensure_session_log_sink(session_id: str, log_dir: str | None = None) -> None:
    _configure_logging()
    if session_id in _SESSION_SINKS:
        _SESSION_SINKS[session_id]["count"] += 1
        return
    target_dir = log_dir or _resolve_session_log_dir()
    os.makedirs(target_dir, exist_ok=True)
    log_path = os.path.join(target_dir, f"{session_id}.log")
    sink_id = loguru_logger.add(
        log_path,
        level=_resolve_log_level(),
        format=_session_log_format(),
        colorize=False,
        filter=lambda record: record["extra"].get("session_id") == session_id,
    )
    _SESSION_SINKS[session_id] = {"id": sink_id, "count": 1}


def _release_session_log_sink(session_id: str) -> None:
    entry = _SESSION_SINKS.get(session_id)
    if not entry:
        return
    entry["count"] -= 1
    if entry["count"] <= 0:
        loguru_logger.remove(entry["id"])
        _SESSION_SINKS.pop(session_id, None)


@contextmanager
def session_log_context(session_id: str, log_dir: str | None = None):
    """Context manager that logs all session output to a session log file."""
    _ensure_session_log_sink(session_id, log_dir=log_dir)
    try:
        with loguru_logger.contextualize(session_id=session_id):
            yield
    finally:
        _release_session_log_sink(session_id)


def get_logger(name: str | None = None):
    """Get the logger for the module."""
    _configure_logging()
    if not name:
        name = __name__
    return loguru_logger.bind(name=name)


def num_tokens_from_string(string: str, encoding_name: str = "cl100k_base") -> int:
    """Get the number of tokens in a string using a specific model."""
    # TODO: Add support for dynamic model selection
    encoding = tiktoken.get_encoding(encoding_name)
    num_tokens = len(encoding.encode(string))
    return num_tokens


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Estimate token count for text using tiktoken.

    Falls back to a rough character-based estimate if encoding lookup fails.
    """
    try:
        enc = tiktoken.encoding_for_model(model)
        return len(enc.encode(text))
    except Exception:
        return len(text) // 4  # Rough fallback


def get_unique_timestamp() -> int:
    """Get a unique timestamp for the task queue."""
    # Get the number of seconds since epoch (Jan 1, 1970) as a float
    current_timestamp = int(time.time())
    # Convert it to string for uniqueness and consistency
    unique_timestamp = str(current_timestamp)
    # Return the integer version of this string timestamp
    return int("".join(str(x) for x in map(int, unique_timestamp)))


def get_system_prompt(name: str = "action-planner") -> str:
    """Get the system prompt for the task queue."""
    logging = get_logger(name="core.common.get_system_prompt")
    prompt_resource = resources.files("meeseeks_core").joinpath("prompts").joinpath(f"{name}.txt")
    with resources.as_file(prompt_resource) as system_prompt_path:
        with open(system_prompt_path, encoding="utf-8") as system_prompt_file:
            system_prompt = system_prompt_file.read()
        logging.debug("Getting system prompt from `{}`", system_prompt_path)
    del logging
    return system_prompt.strip()


_NOLOAD_MARKER = "<!-- meeseeks:noload -->"


@dataclass
class InstructionSource:
    """A single source of project/user instructions."""

    content: str
    path: str
    level: str  # "user", "project", "rules", "local"
    priority: int  # Higher = takes precedence in composition


def _find_git_root(start: Path) -> Path | None:
    """Walk up from start to find the nearest .git directory."""
    current = start.resolve()
    while True:
        if (current / ".git").exists():
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def discover_all_instructions(cwd: str | None = None) -> list[InstructionSource]:
    """Discover instructions from all levels, ordered by priority (lowest first).

    Levels (ascending priority):
    1. User:    ~/.claude/CLAUDE.md (priority 10)
    2. Project: CLAUDE.md, .claude/CLAUDE.md walking up to git root (priority 20-29)
    3. Rules:   .claude/rules/*.md in CWD (priority 30)
    4. Local:   CLAUDE.local.md in CWD (priority 40)
    """
    sources: list[InstructionSource] = []
    work_dir = Path(cwd) if cwd else Path.cwd()

    # 1. User level
    user_claude = Path.home() / ".claude" / "CLAUDE.md"
    if user_claude.is_file():
        content = user_claude.read_text(encoding="utf-8", errors="replace").strip()
        if content and not content.startswith(_NOLOAD_MARKER):
            sources.append(
                InstructionSource(content=content, path=str(user_claude), level="user", priority=10)
            )

    # 2. Project level — walk from CWD up to git root (or filesystem root)
    git_root = _find_git_root(work_dir)
    stop_at = git_root or Path(work_dir.anchor)
    current = work_dir
    depth = 0
    while current >= stop_at:
        for filename in ("CLAUDE.md", ".claude/CLAUDE.md"):
            candidate = current / filename
            if candidate.is_file():
                content = candidate.read_text(encoding="utf-8", errors="replace").strip()
                if content and not content.startswith(_NOLOAD_MARKER):
                    # Closer to CWD = higher priority within project level
                    prio = 20 + min(depth, 9)  # 20 (CWD) to 29 (root)
                    sources.append(
                        InstructionSource(
                            content=content, path=str(candidate), level="project", priority=prio
                        )
                    )
        parent = current.parent
        if parent == current:
            break
        current = parent
        depth += 1

    # 3. Rules level — .claude/rules/*.md in CWD
    rules_dir = work_dir / ".claude" / "rules"
    if rules_dir.is_dir():
        for md_file in sorted(rules_dir.glob("*.md")):
            if md_file.is_file():
                content = md_file.read_text(encoding="utf-8", errors="replace").strip()
                if content and not content.startswith(_NOLOAD_MARKER):
                    sources.append(
                        InstructionSource(
                            content=content, path=str(md_file), level="rules", priority=30
                        )
                    )

    # 4. Local level
    local_claude = work_dir / "CLAUDE.local.md"
    if local_claude.is_file():
        content = local_claude.read_text(encoding="utf-8", errors="replace").strip()
        if content and not content.startswith(_NOLOAD_MARKER):
            sources.append(
                InstructionSource(
                    content=content, path=str(local_claude), level="local", priority=40
                )
            )

    # Sort by priority (lowest first — will be composed in order, higher priority last)
    sources.sort(key=lambda s: s.priority)
    return sources


_MAX_SUBTREE_DEPTH = 5
_INSTRUCTION_FILENAMES = ("CLAUDE.md", "AGENTS.md", ".claude/CLAUDE.md")


def discover_subtree_instructions(
    cwd: str | None = None,
    *,
    max_depth: int = _MAX_SUBTREE_DEPTH,
) -> list[InstructionSource]:
    """Walk DOWN from CWD to find CLAUDE.md and AGENTS.md in subdirectories.

    Returns lightweight ``InstructionSource`` entries with *empty* content.
    The model is made aware these files exist and can read them on demand.
    Respects the ``<!-- meeseeks:noload -->`` marker (checked via first line).
    """
    work_dir = Path(cwd) if cwd else Path.cwd()
    found: list[InstructionSource] = []
    for dirpath, dirnames, _filenames in os.walk(work_dir):
        rel = Path(dirpath).relative_to(work_dir)
        depth = len(rel.parts)
        # Prune hidden dirs and common non-project dirs (must happen before any continue)
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv")
        ]
        if depth == 0:
            continue  # Skip CWD itself — already handled by discover_all_instructions
        if depth > max_depth:
            dirnames.clear()
            continue
        for filename in _INSTRUCTION_FILENAMES:
            candidate = Path(dirpath) / filename
            if candidate.is_file():
                try:
                    first_line = candidate.open(encoding="utf-8", errors="replace").readline()
                except OSError:
                    continue
                if first_line.strip().startswith(_NOLOAD_MARKER):
                    continue
                found.append(
                    InstructionSource(
                        content="",
                        path=str(candidate),
                        level="subtree",
                        priority=50,
                    )
                )
    found.sort(key=lambda s: s.path)
    return found


def get_git_context(cwd: str | None = None, max_status_chars: int = 2000) -> str | None:
    """Gather git context (branch, status, recent commits) for system prompt injection.

    Returns formatted git context string, or None if not in a git repo.
    """
    work_dir = cwd or str(Path.cwd())

    def _run_git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=work_dir,
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None

    branch = _run_git("rev-parse", "--abbrev-ref", "HEAD")
    if branch is None:
        return None  # Not a git repo

    default_branch = _run_git("rev-parse", "--abbrev-ref", "origin/HEAD")
    if default_branch:
        default_branch = default_branch.replace("origin/", "")

    status = _run_git("status", "--short")
    if status and len(status) > max_status_chars:
        status = status[:max_status_chars] + "\n[truncated]"

    recent_log = _run_git("log", "--oneline", "-n", "5")

    parts = [f"Current branch: {branch}"]
    if default_branch:
        parts.append(f"Main branch: {default_branch}")
    if status:
        parts.append(f"\nStatus:\n{status}")
    else:
        parts.append("\nStatus: clean")
    if recent_log:
        parts.append(f"\nRecent commits:\n{recent_log}")

    return "\n".join(parts)


def discover_project_instructions(cwd: str | None = None) -> str | None:
    """Discover and load project instruction files. Uses hierarchical discovery.

    Falls back to legacy AGENTS.md behavior when no hierarchical sources are found.
    Files containing ``<!-- meeseeks:noload -->`` on the first line are skipped.

    Additionally walks the subtree to build a lightweight index of nested
    instruction files so the model knows they exist and can read them on demand.

    Returns the composed instruction text, or ``None`` if no files are found.
    """
    sources = discover_all_instructions(cwd)
    if not sources:
        # Fallback to legacy AGENTS.md behavior
        work_dir = Path(cwd) if cwd else Path.cwd()
        agents_md = work_dir / "AGENTS.md"
        if agents_md.is_file():
            content = agents_md.read_text(encoding="utf-8", errors="replace").strip()
            if content and not content.startswith(_NOLOAD_MARKER):
                return content
        # Even with no direct sources, subtree files may exist — fall through

    # Compose direct sources with section headers
    parts: list[str] = []
    for src in sources:
        header = f"# Instructions ({src.level}: {Path(src.path).name})"
        parts.append(f"{header}\n\n{src.content}")

    # Discover subtree instruction files (index only — no content injection)
    subtree = discover_subtree_instructions(cwd)
    if subtree:
        work_dir = Path(cwd) if cwd else Path.cwd()
        lines = []
        for src in subtree:
            rel = Path(src.path).relative_to(work_dir)
            lines.append(f"- {rel}")
        if sources:
            heading = (
                "# Sub-package instruction files\n\n"
                "The following instruction files exist in subdirectories. "
                "Read them when working on the relevant package."
            )
        else:
            heading = (
                "# No root-level instruction files — read before proceeding\n\n"
                "No CLAUDE.md or AGENTS.md was found at the project root. "
                "The following instruction files exist in subdirectories. "
                "You MUST read the most relevant instruction file before "
                "starting any task — they contain critical project context."
            )
        parts.append(heading + "\n\n" + "\n".join(lines))

    if not parts:
        return None
    return "\n\n---\n\n".join(parts)


def format_tool_input(tool_input: object) -> str:
    """Format a tool input for logs and prompts."""
    if isinstance(tool_input, dict):
        return json.dumps(tool_input, ensure_ascii=True)
    return str(tool_input)


def ha_render_system_prompt(
    all_entities: object | None = None,
    name: str = "homeassistant-set-state",
) -> str:
    """Render the Home Assistant Jinja2 system prompt."""
    if all_entities is not None:
        all_entities = str(all_entities).strip()
    logging = get_logger(name="core.common.render_system_prompt")

    # TODO: Catch and log TemplateNotFound when necessary.
    template_env = Environment(loader=PackageLoader("meeseeks_core", "prompts"))
    template = template_env.get_template(f"{name}.txt")
    logging.debug("Render system prompt for `{}`", name)
    del logging

    return template.render(ALL_ENTITIES=all_entities)
