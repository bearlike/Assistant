#!/usr/bin/env python3
"""On-demand schema fetcher for deferred tools.

When ``agent.tool_search.mode == "on"``, MCP tool schemas are stripped from
the initial ``bind_tools()`` call to save context tokens. The model sees
their names via ``<available-deferred-tools>`` and calls ``tool_search``
to fetch the schemas it actually needs. ``ToolUseLoop`` watches for the
matched names in the tool result and re-binds the model so the matched
tools become invocable.

Result format is designed so the model recognises it from training data —
one ``<function>{...}</function>`` line per match inside a ``<functions>``
block.

**Supplement.** ``ToolRegistry.list_specs()`` is only ONE of the
four populations ``ToolUseLoop._bind_model`` actually binds — the spawn family,
``activate_skill`` and the per-agent SESSION TOOLS are injected directly by the
loop and never appear in the registry, so they were permanently unsearchable
("No deferred tools are registered."). The loop now hands :meth:`run` a
``supplement`` of the exact OpenAI-function schemas it bound directly, built
from the SAME per-instance state as ``_bind_model`` (so a strictly-scoped agent
can never widen its surface via search). These are ALREADY bound, so selecting
one is a harmless no-op that simply hands the model its schema — the same
contract the ``select:`` fallback already gives an already-loaded registry tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_mock_speaker

if TYPE_CHECKING:
    from mewbo_core.tool_registry import ToolRegistry, ToolSpec


_DEFAULT_MAX_RESULTS = 5
_FALLBACK_PROMPT_HINT = (
    "Tool search expects a 'query' string. "
    "Use 'select:name1,name2' for direct fetch or keywords for fuzzy search."
)


@dataclass(frozen=True)
class _Match:
    name: str
    score: float


@dataclass(frozen=True)
class _Searchable:
    """A tool the runner can match + render, independent of its source.

    Both a deferred ``ToolRegistry`` spec and a directly-bound loop tool
    (spawn family / ``activate_skill`` / a session tool, supplied via the
    ``supplement``) normalize to this shape so :meth:`ToolSearchRunner._match`
    and :func:`_render_schema_block` never have to discriminate.
    """

    tool_id: str
    description: str
    parameters: dict[str, object]

    @classmethod
    def from_spec(cls, spec: ToolSpec) -> _Searchable:
        """Normalize a registry spec (schema lives under ``metadata['schema']``)."""
        schema = spec.metadata.get("schema") if isinstance(spec.metadata, dict) else None
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        return cls(tool_id=spec.tool_id, description=spec.description, parameters=schema)

    @classmethod
    def from_openai_tool(cls, tool: object) -> _Searchable | None:
        """Normalize an OpenAI-function schema dict (the loop's bound shape).

        Returns ``None`` for a malformed entry (no ``function.name``) so a
        stray supplement item degrades to a skip rather than a crash.
        """
        fn = tool.get("function") if isinstance(tool, dict) else None
        if not isinstance(fn, dict):
            return None
        name = str(fn.get("name") or "").strip()
        if not name:
            return None
        params = fn.get("parameters")
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}}
        return cls(tool_id=name, description=str(fn.get("description") or ""), parameters=params)


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
    item: _Searchable,
    parts: list[str],
    is_mcp: bool,
    required: list[str],
    optional: list[str],
) -> float:
    """Score a single tool against query terms.

    Weights are tuned so exact part match dominates, description match is a
    tiebreaker. Required terms (``+term``) act as a gate — return 0 if any
    required term is missing.
    """
    desc = item.description.lower()
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


def _render_schema_block(items: list[_Searchable]) -> str:
    """Render matched tools as a ``<functions>`` block.

    One ``<function>{...}</function>`` line per tool, JSON-encoded with
    name / description / parameters — same encoding the model sees for
    tools listed at the top of the prompt.
    """
    if not items:
        return "No matching deferred tools found."
    lines = ["<functions>"]
    for item in items:
        payload = {
            "name": item.tool_id,
            "description": item.description,
            "parameters": item.parameters,
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

    def run(
        self,
        action_step: ActionStep,
        *,
        supplement: list[dict[str, object]] | None = None,
    ) -> MockSpeaker:
        """Return matched tool schemas as a ``<functions>`` block.

        *supplement* is the loop's directly-bound OpenAI-function schemas
        (spawn family / ``activate_skill`` / session tools) — searchable
        alongside the deferred registry specs so the model can discover a
        tool it holds but the registry never listed. ``None``
        (the default, e.g. a direct registry invocation) preserves the
        registry-only behaviour byte-for-byte.
        """
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

        from mewbo_core.tool_registry import is_deferred

        population = [
            _Searchable.from_spec(s) for s in self._registry.list_specs() if is_deferred(s)
        ]
        for tool in supplement or []:
            item = _Searchable.from_openai_tool(tool)
            if item is not None:
                population.append(item)
        if not population:
            return speaker(content="No deferred tools are registered.")

        matched = self._match(query, population, max_results)
        return speaker(content=_render_schema_block(matched))

    def _match(
        self, query: str, population: list[_Searchable], max_results: int
    ) -> list[_Searchable]:
        """Resolve ``query`` to a list of matched tools, capped at ``max_results``."""
        # Direct selection: ``select:name1,name2``.
        if query.lower().startswith("select:"):
            wanted = [s.strip() for s in query[7:].split(",") if s.strip()]
            by_id = {s.tool_id: s for s in population}
            # Fall back to the full registry — selecting an already-loaded
            # tool (or a supplemented, already-bound one) is a harmless no-op
            # that lets the model proceed with the schema in hand.
            full = {s.tool_id: _Searchable.from_spec(s) for s in self._registry.list_specs()}
            picked: list[_Searchable] = []
            seen: set[str] = set()
            for name in wanted:
                item = by_id.get(name) or full.get(name)
                if item is not None and item.tool_id not in seen:
                    picked.append(item)
                    seen.add(item.tool_id)
            return picked

        # Exact-name fast path: model dropped the ``select:`` prefix.
        for item in population:
            if item.tool_id.lower() == query.lower():
                return [item]

        # Keyword search.
        terms = [t for t in query.lower().split() if t]
        required = [t[1:] for t in terms if t.startswith("+") and len(t) > 1]
        optional = [t for t in terms if not t.startswith("+")]
        if not required and not optional:
            return []

        scored: list[_Match] = []
        for item in population:
            parts, is_mcp = _parse_tool_name(item.tool_id)
            score = _score_spec(item, parts, is_mcp, required, optional)
            if score > 0:
                scored.append(_Match(name=item.tool_id, score=score))
        scored.sort(key=lambda m: m.score, reverse=True)
        winners = {m.name for m in scored[:max_results]}
        return [s for s in population if s.tool_id in winners][:max_results]


__all__ = ["ToolSearchRunner"]
