"""``wiki_load_grounder`` SessionTool — reads optional structure grounder from the clone."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki.clone import _resolve_runtime  # noqa: F401 — per-module test seam

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.grounder")

# ---------------------------------------------------------------------------
# Default discovery paths (priority order — first match wins)
# ---------------------------------------------------------------------------

_DEFAULT_GROUNDER_PATHS: tuple[str, ...] = (
    ".mewbo/wiki.json",
    ".devin/wiki.json",
)


# ---------------------------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------------------------


class GrounderPage(BaseModel):
    """A page hint in the wiki grounder."""

    model_config = ConfigDict(extra="forbid")

    title: str
    purpose: str = ""
    parent: str | None = None


class GrounderNote(BaseModel):
    """A free-form repo note in the wiki grounder."""

    model_config = ConfigDict(extra="forbid")

    content: str


class WikiGrounder(BaseModel):
    """Parsed contents of a ``.mewbo/wiki.json`` or ``.devin/wiki.json`` grounder file."""

    model_config = ConfigDict(extra="forbid")

    repo_notes: list[GrounderNote] = Field(default_factory=list)
    pages: list[GrounderPage] = Field(default_factory=list)


class WikiLoadGrounderArgs(BaseModel):
    """Arguments for ``wiki_load_grounder`` — none required; discovery is from ctx.clone_dir."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiLoadGrounderTool(WikiSessionTool):
    """SessionTool: discover and parse the optional repo structure grounder."""

    tool_id = "wiki_load_grounder"
    args_cls = WikiLoadGrounderArgs
    schema: dict[str, object] = pydantic_to_openai_tool(
        WikiLoadGrounderArgs, name="wiki_load_grounder"
    )

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_load_grounder`` tool call."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse args (no fields, but validate extra=forbid).
        parsed = self._parse_args(WikiLoadGrounderArgs, action_step)
        if isinstance(parsed, MockSpeaker):
            return parsed

        # 3. Discover grounder file.
        clone_dir = ctx.clone_dir
        result = _find_and_load(clone_dir, _DEFAULT_GROUNDER_PATHS)
        if isinstance(result, MockSpeaker):
            # Propagate validation error upward.
            return result

        if result is None:
            return MockSpeaker(content=str({"grounder": None}))

        grounder, rel_path = result
        return MockSpeaker(content=str({
            "grounder": grounder.model_dump(),
            "source_path": rel_path,
        }))


# ---------------------------------------------------------------------------
# Discovery helper (pure function — easy to test in isolation)
# ---------------------------------------------------------------------------


def _find_and_load(
    clone_dir: Path,
    paths: tuple[str, ...],
) -> tuple[WikiGrounder, str] | MockSpeaker | None:
    """Try each candidate path in order.

    Returns:
    - ``(WikiGrounder, rel_path)`` on success.
    - ``MockSpeaker`` carrying a validation error if a file exists but is invalid.
    - ``None`` if no grounder file was found.
    """
    for rel in paths:
        candidate = clone_dir / rel
        if not candidate.exists():
            continue

        raw_text = candidate.read_text(encoding="utf-8")

        # JSON parse
        try:
            data: Any = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _err_result(
                "validation",
                f"Invalid JSON in {rel}: {exc}",
            )

        # Schema validate
        try:
            grounder = WikiGrounder.model_validate(data)
        except ValidationError as ve:
            return _err_result(
                "validation",
                f"Schema mismatch in {rel}: {ve}",
            )

        return grounder, rel

    return None


__all__ = [
    "GrounderNote",
    "GrounderPage",
    "WikiGrounder",
    "WikiLoadGrounderArgs",
    "WikiLoadGrounderTool",
]
