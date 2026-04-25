#!/usr/bin/env python3
"""On-demand schema fetcher for deferred tools.

When ``agent.tool_search.mode == "on"``, MCP tool schemas are stripped from
the initial ``bind_tools()`` call to save context tokens. The model sees
their names via ``<available-deferred-tools>`` and calls ``tool_search``
to fetch the schemas it actually needs. ``ToolUseLoop`` watches for the
matched names in the tool result and re-binds the model so the matched
tools become invocable.

Result format mirrors Claude Code's ``ToolSearchTool`` so the model
recognises it from training data — one ``<function>{...}</function>``
line per match inside a ``<functions>`` block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from truss_core.classes import ActionStep
from truss_core.common import MockSpeaker, get_mock_speaker

if TYPE_CHECKING:
    from truss_core.tool_registry import ToolRegistry, ToolSpec


_DEFAULT_MAX_RESULTS = 5
_FALLBACK_PROMPT_HINT = (
    "Tool search expects a 'query' string. "
    "Use 'select:name1,name2' for direct fetch or keywords for fuzzy search."
)


@dataclass(frozen=True)
class _Match:
    name: str
    score: float


def _parse_tool_name(name: str) -> tuple[list[str], bool]:
    """Split a tool name into searchable parts.

    MCP tools use ``mcp__server__action``; built-ins use snake_case or
    CamelCase. Returns ``(parts_lowercase, is_mcp)``.
    """
    if name.startswith("mcp__"):
        rest = name[5:].lower()
        parts = [p for chunk in rest.split("__") for p in chunk.split("_") if p]
        return parts, True
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", name).replace("_", " ").lower()
    return [p for p in spaced.split() if p], False


def _score_spec(
    spec: ToolSpec,
    parts: list[str],
    is_mcp: bool,
    required: list[str],
    optional: list[str],
) -> float:
    """Score a single spec against query terms.

    Weights mirror Claude Code's tuned values: exact part match dominates,
    description match is a tiebreaker. Required terms (``+term``) act as a
    gate — return 0 if any required term is missing.
    """
    desc = spec.description.lower()
    all_terms = [*required, *optional]
    score = 0.0
    for term in required:
        in_parts = any(term == part or term in part for part in parts)
        in_desc = bool(re.search(rf"\b{re.escape(term)}\b", desc))
        if not (in_parts or in_desc):
            return 0.0
    for term in all_terms:
        if term in parts:
            score += 12.0 if is_mcp else 10.0
        elif any(term in part for part in parts):
            score += 6.0 if is_mcp else 5.0
        if re.search(rf"\b{re.escape(term)}\b", desc):
            score += 2.0
    return score


def _render_schema_block(specs: list[ToolSpec]) -> str:
    """Render matched specs as a ``<functions>`` block.

    One ``<function>{...}</function>`` line per spec, JSON-encoded with
    name / description / parameters — same encoding the model sees for
    tools listed at the top of the prompt.
    """
    if not specs:
        return "No matching deferred tools found."
    lines = ["<functions>"]
    for spec in specs:
        schema = spec.metadata.get("schema") if isinstance(spec.metadata, dict) else None
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        payload = {
            "name": spec.tool_id,
            "description": spec.description,
            "parameters": schema,
        }
        lines.append(f"<function>{json.dumps(payload, ensure_ascii=False)}</function>")
    lines.append("</functions>")
    return "\n".join(lines)


def _coerce_int(value: object, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


class ToolSearchRunner:
    """Resolve deferred tool schemas on demand.

    Reads ``ToolRegistry`` lazily so the latest specs (including MCP tools
    that connected after registry construction) are searchable.
    """

    def __init__(self, registry: ToolRegistry) -> None:
        """Bind to ``registry`` so each call sees the current spec set."""
        self._registry = registry

    def run(self, action_step: ActionStep) -> MockSpeaker:
        """Return matched tool schemas as a ``<functions>`` block."""
        speaker = get_mock_speaker()
        argument = action_step.tool_input
        if isinstance(argument, str):
            query = argument.strip()
            max_results = _DEFAULT_MAX_RESULTS
        elif isinstance(argument, dict):
            query = str(argument.get("query") or "").strip()
            max_results = _coerce_int(argument.get("max_results"), _DEFAULT_MAX_RESULTS)
        else:
            return speaker(content=_FALLBACK_PROMPT_HINT)
        if not query:
            return speaker(content=_FALLBACK_PROMPT_HINT)
        max_results = max(1, min(max_results, 25))

        from truss_core.tool_registry import is_deferred

        deferred = [s for s in self._registry.list_specs() if is_deferred(s)]
        if not deferred:
            return speaker(content="No deferred tools are registered.")

        matched = self._match(query, deferred, max_results)
        return speaker(content=_render_schema_block(matched))

    def _match(self, query: str, deferred: list[ToolSpec], max_results: int) -> list[ToolSpec]:
        """Resolve ``query`` to a list of matched specs, capped at ``max_results``."""
        # Direct selection: ``select:name1,name2``.
        if query.lower().startswith("select:"):
            wanted = [s.strip() for s in query[7:].split(",") if s.strip()]
            by_id = {s.tool_id: s for s in deferred}
            # Fall back to the full registry — selecting an already-loaded
            # tool is a harmless no-op that lets the model proceed.
            full = {s.tool_id: s for s in self._registry.list_specs()}
            picked: list[ToolSpec] = []
            seen: set[str] = set()
            for name in wanted:
                spec = by_id.get(name) or full.get(name)
                if spec is not None and spec.tool_id not in seen:
                    picked.append(spec)
                    seen.add(spec.tool_id)
            return picked

        # Exact-name fast path: model dropped the ``select:`` prefix.
        for spec in deferred:
            if spec.tool_id.lower() == query.lower():
                return [spec]

        # Keyword search.
        terms = [t for t in query.lower().split() if t]
        required = [t[1:] for t in terms if t.startswith("+") and len(t) > 1]
        optional = [t for t in terms if not t.startswith("+")]
        if not required and not optional:
            return []

        scored: list[_Match] = []
        for spec in deferred:
            parts, is_mcp = _parse_tool_name(spec.tool_id)
            score = _score_spec(spec, parts, is_mcp, required, optional)
            if score > 0:
                scored.append(_Match(name=spec.tool_id, score=score))
        scored.sort(key=lambda m: m.score, reverse=True)
        winners = {m.name for m in scored[:max_results]}
        return [s for s in deferred if s.tool_id in winners][:max_results]


__all__ = ["ToolSearchRunner"]
