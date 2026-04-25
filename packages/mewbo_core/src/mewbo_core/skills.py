#!/usr/bin/env python3
"""Skill discovery, registry, and activation for the Mewbo assistant.

A *skill* is a ``SKILL.md`` file (YAML frontmatter + markdown body) that
teaches the LLM how to perform a specific task.  Skills are **not** tools —
they are instruction sets that modify the system prompt and optionally scope
the available tools.

Discovery locations (later overrides earlier by name):

1. Personal: ``~/.claude/skills/<name>/SKILL.md``
2. Project:  ``<cwd>/.claude/skills/<name>/SKILL.md``
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml  # type: ignore[import-untyped]

from mewbo_core.capabilities import (
    filter_by_capabilities,
    overlay_capabilities,
    parse_capabilities,
)
from mewbo_core.common import get_logger

if TYPE_CHECKING:
    from mewbo_core.tool_registry import ToolSpec

logging = get_logger(name="core.skills")

# ------------------------------------------------------------------
# Skill data model
# ------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,62}[a-z0-9]?$")
_MAX_DESCRIPTION_LEN = 1024
_SHELL_PATTERN = re.compile(r"!\`([^`]+)\`")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass(frozen=True)
class SkillSpec:
    """Metadata and content of a discovered skill."""

    name: str
    description: str
    source_path: str
    source: str  # "personal" or "project"
    allowed_tools: list[str] | None = None
    disable_model_invocation: bool = False
    user_invocable: bool = True
    context: str | None = None  # "fork" or None (inline)
    agent: str | None = None
    model: str | None = None
    body: str = ""
    mtime: float = 0.0
    requires_capabilities: tuple[str, ...] = ()


# ------------------------------------------------------------------
# Internal tool schema (injected into bind_tools like SPAWN_AGENT_SCHEMA)
# ------------------------------------------------------------------

ACTIVATE_SKILL_SCHEMA: dict[str, object] = {
    "type": "function",
    "function": {
        "name": "activate_skill",
        "description": (
            "Activate a skill by name to receive specialized instructions "
            "for the task. Use this when the user's request matches an "
            "available skill."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {
                    "type": "string",
                    "description": "Name of the skill to activate",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill",
                },
            },
            "required": ["skill_name"],
        },
    },
}


# ------------------------------------------------------------------
# Discovery & parsing
# ------------------------------------------------------------------


def _parse_skill_file(
    path: Path, source: str, *, default_name: str | None = None
) -> SkillSpec | None:
    """Parse a SKILL.md file into a SkillSpec, or ``None`` on failure."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logging.warning("Failed to read {}: {}", path, exc)
        return None

    match = _FRONTMATTER_RE.match(raw)
    if match is None:
        logging.warning("No YAML frontmatter in {}", path)
        return None

    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        logging.warning("Invalid YAML in {}: {}", path, exc)
        return None

    if not isinstance(meta, dict):
        logging.warning("Frontmatter is not a mapping in {}", path)
        return None

    name = meta.get("name") or default_name
    description = meta.get("description")

    if not name or not isinstance(name, str):
        logging.warning("Missing or invalid 'name' in {}", path)
        return None
    if not description or not isinstance(description, str):
        logging.warning("Missing or invalid 'description' in {}", path)
        return None

    name = name.strip()
    if not _NAME_RE.match(name):
        logging.warning(
            "Invalid skill name '{}' in {} (must be lowercase, hyphens, max 64 chars)",
            name,
            path,
        )
        return None

    description = description.strip()[:_MAX_DESCRIPTION_LEN]

    # Parse allowed-tools (space-delimited string → list).
    allowed_tools_raw = meta.get("allowed-tools")
    allowed_tools: list[str] | None = None
    if isinstance(allowed_tools_raw, str) and allowed_tools_raw.strip():
        allowed_tools = allowed_tools_raw.strip().split()
    elif isinstance(allowed_tools_raw, list):
        allowed_tools = [str(t) for t in allowed_tools_raw if t]

    body = raw[match.end() :]

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0

    # Capability gating — same two-key contract as AgentDef.
    _raw_list = meta.get("requires-capabilities")
    list_form: list = _raw_list if isinstance(_raw_list, list) else []
    _raw_scalar = meta.get("requires-capability")
    scalar_form: str = _raw_scalar if isinstance(_raw_scalar, str) else ""
    requires_capabilities = parse_capabilities([*list_form, scalar_form])

    return SkillSpec(
        name=name,
        description=description,
        source_path=str(path),
        source=source,
        allowed_tools=allowed_tools or None,
        disable_model_invocation=bool(meta.get("disable-model-invocation", False)),
        user_invocable=bool(meta.get("user-invocable", True)),
        context=str(meta["context"]) if meta.get("context") else None,
        agent=str(meta["agent"]) if meta.get("agent") else None,
        model=str(meta["model"]) if meta.get("model") else None,
        body=body,
        mtime=mtime,
        requires_capabilities=requires_capabilities,
    )


_MAX_SKILL_DEPTH = 5


def discover_skills(cwd: str | None = None) -> list[SkillSpec]:
    """Discover skills from personal, project, and subtree directories.

    Priority (later overrides earlier for same name):
    1. Personal: ``~/.claude/skills/*/SKILL.md``
    2. Project:  ``<cwd>/.claude/skills/*/SKILL.md``
    3. Subtree:  ``<cwd>/**/.claude/skills/*/SKILL.md`` (max depth 5, no override)
    """
    skills: dict[str, SkillSpec] = {}

    # 1. Personal: ~/.claude/skills/*/SKILL.md
    personal_dir = Path.home() / ".claude" / "skills"
    if personal_dir.is_dir():
        for child in sorted(personal_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.is_file():
                spec = _parse_skill_file(
                    skill_file,
                    source="personal",
                    default_name=child.name,
                )
                if spec is not None:
                    skills[spec.name] = spec

    # 2. Project: <cwd>/.claude/skills/*/SKILL.md
    base = Path(cwd) if cwd else Path.cwd()
    project_dir = base / ".claude" / "skills"
    if project_dir.is_dir():
        for child in sorted(project_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.is_file():
                spec = _parse_skill_file(
                    skill_file,
                    source="project",
                    default_name=child.name,
                )
                if spec is not None:
                    skills[spec.name] = spec  # project overrides personal

    # 3. Subtree: walk down from CWD to find .claude/skills/ directories
    _discover_subtree_skills(base, skills, max_depth=_MAX_SKILL_DEPTH)

    return list(skills.values())


def _discover_subtree_skills(
    root: Path,
    skills: dict[str, SkillSpec],
    *,
    max_depth: int = _MAX_SKILL_DEPTH,
) -> None:
    """Walk DOWN from *root* to discover ``.claude/skills/`` in subdirectories.

    Subtree skills do **not** override personal or project-root skills.
    """
    for dirpath, dirnames, _filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        # Prune non-project dirs (must happen before any continue)
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".") and d not in ("node_modules", "__pycache__", ".venv", "venv")
        ]
        if depth > max_depth:
            dirnames.clear()
            continue
        # Check for .claude/skills/ at this level
        skills_dir = Path(dirpath) / ".claude" / "skills"
        if not skills_dir.is_dir():
            continue
        if depth == 0:
            continue  # Skip CWD — already handled by phase 2
        for child in sorted(skills_dir.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.is_file():
                spec = _parse_skill_file(
                    skill_file,
                    source="project",
                    default_name=child.name,
                )
                if spec is not None:
                    # Subtree skills DON'T override project-root or personal skills
                    if spec.name not in skills:
                        skills[spec.name] = spec


# ------------------------------------------------------------------
# Skill registry
# ------------------------------------------------------------------


class SkillRegistry:
    """Registry of discovered skills with hot-reload support."""

    def __init__(self) -> None:  # noqa: D107
        self._skills: dict[str, SkillSpec] = {}
        self._cwd: str | None = None

    def load(self, cwd: str | None = None) -> None:
        """Discover and load all skills."""
        self._cwd = cwd
        for spec in discover_skills(cwd):
            self._skills[spec.name] = spec
        if self._skills:
            logging.info("Loaded {} skill(s)", len(self._skills))

    def get(
        self,
        name: str,
        session_capabilities: Iterable[str] = (),
    ) -> SkillSpec | None:
        """Get a skill by name.

        Skills gated by ``requires_capabilities`` that the session hasn't
        advertised are treated as if they don't exist.
        """
        skill = self._skills.get(name)
        if skill is None:
            return None
        visible = filter_by_capabilities([skill], session_capabilities)
        return visible[0] if visible else None

    def list_all(self) -> list[SkillSpec]:
        """List all discovered skills."""
        return list(self._skills.values())

    def visible_for(self, session_capabilities: Iterable[str]) -> list[SkillSpec]:
        """Return skills visible given the session's advertised capabilities."""
        return filter_by_capabilities(self._skills.values(), session_capabilities)

    def list_user_invocable(self, session_capabilities: Iterable[str] = ()) -> list[SkillSpec]:
        """List skills available for user slash-command invocation."""
        return [s for s in self.visible_for(session_capabilities) if s.user_invocable]

    def list_auto_invocable(self, session_capabilities: Iterable[str] = ()) -> list[SkillSpec]:
        """List skills the LLM can auto-invoke."""
        return [s for s in self.visible_for(session_capabilities) if not s.disable_model_invocation]

    def render_catalog(self, session_capabilities: Iterable[str] = ()) -> str:
        """Render a compact skill catalog for system prompt injection.

        Applies capability filtering before rendering.
        """
        auto = self.list_auto_invocable(session_capabilities)
        if not auto:
            return ""
        lines = ["Available skills (use activate_skill to load instructions when relevant):"]
        for skill in auto:
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def maybe_reload(self) -> bool:
        """Check if any skill files changed and reload if so.

        Returns True if any skills were reloaded.
        """
        changed = False
        # Check existing skills for mtime changes.
        for name, spec in list(self._skills.items()):
            try:
                current_mtime = Path(spec.source_path).stat().st_mtime
            except OSError:
                # File deleted — remove from registry.
                del self._skills[name]
                changed = True
                continue
            if current_mtime != spec.mtime:
                reloaded = _parse_skill_file(Path(spec.source_path), spec.source)
                if reloaded is not None:
                    self._skills[reloaded.name] = reloaded
                else:
                    del self._skills[name]
                changed = True

        # Also check for new skill directories that appeared since last scan.
        fresh = discover_skills(self._cwd)
        for spec in fresh:
            if spec.name not in self._skills:
                self._skills[spec.name] = spec
                changed = True

        if changed:
            logging.info("Skills reloaded ({} active)", len(self._skills))
        return changed

    def load_extra_dir(
        self,
        skills_dir: str,
        source: str = "plugin",
        *,
        requires_capabilities: tuple[str, ...] = (),
    ) -> None:
        """Load skills from an extra directory. Does NOT override existing.

        When ``requires_capabilities`` is non-empty (e.g. the plugin manifest
        declared bundle-level capabilities), the values are unioned onto each
        loaded spec's own ``requires_capabilities``.
        """
        base = Path(skills_dir)
        if not base.is_dir():
            return
        for child in sorted(base.iterdir()):
            skill_file = child / "SKILL.md"
            if child.is_dir() and skill_file.is_file():
                spec = _parse_skill_file(
                    skill_file,
                    source=source,
                    default_name=child.name,
                )
                if spec is None:
                    continue
                spec = overlay_capabilities(spec, requires_capabilities)
                if spec.name not in self._skills:
                    self._skills[spec.name] = spec

    def load_command_file(
        self,
        path: str,
        source: str = "plugin",
        *,
        requires_capabilities: tuple[str, ...] = (),
    ) -> None:
        """Load a flat commands/*.md file as a skill."""
        p = Path(path)
        if not p.is_file():
            return
        spec = _parse_skill_file(p, source=source, default_name=p.stem)
        if spec is None:
            return
        spec = overlay_capabilities(spec, requires_capabilities)
        if spec.name not in self._skills:
            self._skills[spec.name] = spec

    def load_plugin_components(self, fan_out: Any) -> None:
        """Merge skill_dirs + command_files from a ``PluginFanOut``.

        Single point of truth for "what skills do plugins contribute?" so the
        orchestrator (which actually runs queries) and the CLI (which lists +
        dispatches user-typed ``/<skill>`` commands) see an identical set.
        """
        for pc in fan_out.components:
            if pc.manifest is None:
                continue
            plugin_source = f"plugin:{pc.manifest.name}"
            plugin_caps = pc.manifest.requires_capabilities
            for sd in pc.skill_dirs:
                self.load_extra_dir(sd, source=plugin_source, requires_capabilities=plugin_caps)
            for cf in pc.command_files:
                self.load_command_file(cf, source=plugin_source, requires_capabilities=plugin_caps)


# ------------------------------------------------------------------
# Shell preprocessing
# ------------------------------------------------------------------


def _preprocess_shell(body: str) -> str:
    r"""Replace ``!\`command\`` patterns with their stdout.

    Each matched command is executed via ``subprocess.run(shell=True)`` with
    a 30-second timeout.  On error the pattern is replaced with an
    ``[ERROR: ...]`` placeholder.
    """

    def _run(match: re.Match[str]) -> str:
        cmd = match.group(1)
        try:
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            return (
                result.stdout.strip()
                if result.returncode == 0
                else (
                    f"[ERROR: command exited {result.returncode}: "
                    f"{result.stderr.strip() or result.stdout.strip()}]"
                )
            )
        except subprocess.TimeoutExpired:
            return f"[ERROR: command timed out after 30s: {cmd}]"
        except OSError as exc:
            return f"[ERROR: {exc}]"

    return _SHELL_PATTERN.sub(_run, body)


# ------------------------------------------------------------------
# Skill activation
# ------------------------------------------------------------------


def activate_skill(
    skill: SkillSpec,
    args: str = "",
    tool_specs: list[ToolSpec] | None = None,
) -> tuple[str, list[ToolSpec] | None]:
    """Activate a skill: render instructions and scope tools.

    Returns ``(rendered_instructions, scoped_tool_specs)``.
    ``scoped_tool_specs`` is ``None`` when the skill has no tool restrictions.
    """
    body = skill.body

    # Argument substitution.
    arg_parts = args.split() if args else []
    body = body.replace("$ARGUMENTS", args)
    for i, part in enumerate(arg_parts):
        body = body.replace(f"${i}", part)

    # Shell preprocessing.
    body = _preprocess_shell(body)

    # Tool scoping.
    scoped_specs: list[ToolSpec] | None = None
    if skill.allowed_tools and tool_specs is not None:
        from mewbo_core.tool_registry import filter_specs

        scoped_specs = filter_specs(tool_specs, allowed=skill.allowed_tools)
    elif tool_specs is not None:
        scoped_specs = tool_specs

    return body, scoped_specs


__all__ = [
    "ACTIVATE_SKILL_SCHEMA",
    "SkillRegistry",
    "SkillSpec",
    "activate_skill",
    "discover_skills",
]
