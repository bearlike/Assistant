#!/usr/bin/env python3
"""Agent definition registry for the Meeseeks assistant.

An *agent definition* is an ``agents/*.md`` file (YAML frontmatter + markdown body)
loaded from a Claude Code plugin or personal/project directory.  Agent definitions
tell Meeseeks what sub-agents are available, what tools they may use, and what their
system prompt should be.

This module mirrors the structure of ``skills.py`` — same frontmatter regex, same
frozen dataclass pattern, same registry pattern with no-override semantics.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

from meeseeks_core.common import get_logger

logging = get_logger(name="core.agents")

# ------------------------------------------------------------------
# Frontmatter parsing (same pattern as skills.py)
# ------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# ------------------------------------------------------------------
# CC → Meeseeks tool name mapping
# ------------------------------------------------------------------

CC_TOOL_MAP: dict[str, str] = {
    "Read": "read_file",
    "Glob": "aider_list_dir_tool",
    "Grep": "aider_shell_tool",  # grep is done via shell
    "Bash": "aider_shell_tool",
    "BashOutput": "aider_shell_tool",
    "KillShell": "aider_shell_tool",
    "Edit": "aider_edit_block_tool",
    "Write": "file_edit_tool",
    "LS": "aider_list_dir_tool",
    "NotebookRead": "read_file",
    "NotebookEdit": "file_edit_tool",
    "WebFetch": "aider_shell_tool",
    "WebSearch": "aider_shell_tool",
    "TodoWrite": "aider_shell_tool",
}


def map_cc_tool_names(cc_names: list[str]) -> list[str]:
    """Map a list of CC tool names to Meeseeks tool IDs.

    Unknown names pass through unchanged.  Duplicates are removed while
    preserving first-occurrence order (multiple CC names may map to the same
    Meeseeks tool ID).
    """
    seen: set[str] = set()
    result: list[str] = []
    for name in cc_names:
        mapped = CC_TOOL_MAP.get(name, name)
        if mapped not in seen:
            seen.add(mapped)
            result.append(mapped)
    return result


# ------------------------------------------------------------------
# Agent data model
# ------------------------------------------------------------------


@dataclass(frozen=True)
class AgentDef:
    """An agent definition loaded from an agents/*.md file."""

    name: str
    description: str
    source_path: str  # absolute path to the .md file
    source: str  # "plugin:<plugin-name>" or "project" or "personal"
    body: str  # markdown body (becomes agent's system prompt)
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    model: str | None = None  # "inherit" becomes None


# ------------------------------------------------------------------
# File parsing
# ------------------------------------------------------------------


def parse_agent_file(path: Path, source: str) -> AgentDef | None:
    """Parse an ``agents/*.md`` file into an :class:`AgentDef`, or ``None`` on failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logging.warning("Failed to read {}: {}", path, exc)
        return None

    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        # No frontmatter — infer a minimal agent def from the markdown body.
        # Third-party plugins (e.g. slack-block-kit-builder) sometimes ship
        # agent files as plain markdown without YAML frontmatter.
        logging.debug("No YAML frontmatter in {} — inferring from content", path)
        name = path.stem
        # Use first non-blank line (stripped of # prefix) as description
        description = ""
        for line in raw.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                description = stripped[:200]
                break
        return AgentDef(
            name=name,
            description=description,
            source_path=str(path),
            source=source,
            body=raw,
        )

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logging.warning("Invalid YAML in {}: {}", path, exc)
        return None

    if not isinstance(meta, dict):
        logging.warning("Frontmatter is not a mapping in {}", path)
        return None

    # Name: required, fall back to filename stem
    name_raw = meta.get("name")
    name = name_raw if isinstance(name_raw, str) and name_raw.strip() else path.stem
    name = name.strip()

    # Description: "description" or "when-to-use" alias
    description = meta.get("description") or meta.get("when-to-use") or ""
    if isinstance(description, str):
        description = description.strip()
    else:
        description = ""

    # Allowed tools: space-delimited string or YAML list → mapped via CC_TOOL_MAP
    allowed_tools: list[str] | None = None
    tools_raw = meta.get("tools")
    if isinstance(tools_raw, str) and tools_raw.strip():
        allowed_tools = map_cc_tool_names(tools_raw.strip().split())
    elif isinstance(tools_raw, list):
        allowed_tools = map_cc_tool_names([str(t) for t in tools_raw if t])

    # Denied tools: same parsing
    denied_tools: list[str] | None = None
    disallowed_raw = meta.get("disallowedTools")
    if isinstance(disallowed_raw, str) and disallowed_raw.strip():
        denied_tools = map_cc_tool_names(disallowed_raw.strip().split())
    elif isinstance(disallowed_raw, list):
        denied_tools = map_cc_tool_names([str(t) for t in disallowed_raw if t])

    # Model: "inherit" → None
    model_raw = meta.get("model")
    model: str | None = None
    if isinstance(model_raw, str) and model_raw.strip() and model_raw.strip() != "inherit":
        model = model_raw.strip()

    # Body: everything after the closing frontmatter ---
    body = raw[match.end() :]

    return AgentDef(
        name=name,
        description=description,
        source_path=str(path),
        source=source,
        body=body,
        allowed_tools=allowed_tools or None,
        denied_tools=denied_tools or None,
        model=model,
    )


# ------------------------------------------------------------------
# Agent registry
# ------------------------------------------------------------------


class AgentRegistry:
    """Registry of agent definitions.

    First-registered agent wins — later registrations with the same name are
    silently ignored (same semantics as the subtree skill discovery).
    """

    def __init__(self) -> None:  # noqa: D107
        self._agents: dict[str, AgentDef] = {}

    def register(self, agent_def: AgentDef) -> None:
        """Register an agent.  Does NOT override existing entries."""
        if agent_def.name not in self._agents:
            self._agents[agent_def.name] = agent_def

    def get(self, name: str) -> AgentDef | None:
        """Return the agent definition with the given name, or ``None``."""
        return self._agents.get(name)

    def list_all(self) -> list[AgentDef]:
        """Return all registered agent definitions."""
        return list(self._agents.values())

    def render_catalog(self) -> str:
        """Render a compact agent catalog for system prompt injection."""
        if not self._agents:
            return ""
        lines = ["Available agent types (use spawn_agent with agent_type to delegate):"]
        for agent in self._agents.values():
            lines.append(f"- {agent.name}: {agent.description}")
        return "\n".join(lines)


__all__ = [
    "AgentDef",
    "AgentRegistry",
    "CC_TOOL_MAP",
    "map_cc_tool_names",
    "parse_agent_file",
]
