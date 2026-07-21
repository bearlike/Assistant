#!/usr/bin/env python3
"""Dedicated transcript cards for the built-in orchestration tools.

The hypervisor tools — ``spawn_agent`` / ``spawn_agents`` (sub-agent launch),
``check_agents`` (fleet status), and ``tool_search`` (deferred-schema discovery)
— otherwise fall through to the generic tool renderer and dump raw JSON envelopes
into the transcript. This module renders them as compact, legible cards instead.

It is **additive and self-contained**: it never edits the transcript render
dispatch (which the foundation rewrite owns). Instead :func:`register_orchestration_cards`
installs ONE wrapping ``"tool"`` renderer that draws a card for the three
orchestration ``tool_id``s and delegates every other tool to the canonical base
renderer (reached through a private staging registry, mirroring the idiom in
``TranscriptView.on_mount``). Colours + glyphs come exclusively from the injected
:class:`~mewbo_cli.cli_theme.Palette` and :data:`~mewbo_cli.cli_icons.ICONS` — no
hardcoded hex, no new look invented (the ``t-tool`` left-rail still applies).
"""

from __future__ import annotations

import json
import re
from typing import Any

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem
from mewbo_cli.tui.transcript_render import register_transcript_renderers

# The orchestration tool_ids this module draws cards for. Everything else falls
# through to the canonical base tool renderer.
_SPAWN_IDS = frozenset({"spawn_agent", "spawn_agents"})
_FLEET_IDS = frozenset({"check_agents"})
_SEARCH_IDS = frozenset({"tool_search"})
ORCHESTRATION_TOOL_IDS = _SPAWN_IDS | _FLEET_IDS | _SEARCH_IDS

# Truncation caps — keep a card compact regardless of how verbose the LLM was.
_TASK_MAX = 200
_DESC_MAX = 100
_MAX_BATCH_ROWS = 8
_MAX_SEARCH_ROWS = 12

# A failed-looking status drives the ✗ glyph + error colour.
_FAIL_STATES = frozenset({"failed", "rejected", "cancelled", "error", "cannot_solve"})


def _truncate(text: str, limit: int) -> str:
    """Collapse whitespace and truncate ``text`` to ``limit`` chars with an ellipsis."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _parse_result(result: object) -> dict[str, Any] | None:
    """Return the tool result as a dict, parsing a JSON string when needed.

    Returns ``None`` when ``result`` is absent or not a JSON object (e.g. a plain
    ``"ERROR: …"`` string), so callers can degrade gracefully. Never raises.
    """
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    stripped = result.strip()
    if not stripped.startswith("{"):
        return None
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _summary_value(args_summary: object, key: str) -> str | None:
    """Pull ``key`` out of a ``"k=v, k=v"`` args-summary string, else ``None``.

    The compact ``args_summary`` (built by ``TurnEngine._args_summary``) is the
    only payload carrier for a spawn's ``model`` / ``agent_type``. Their values
    never contain ``", "`` so a split-based parse is reliable; a value that does
    is simply not surfaced (graceful degradation, no elaborate fallback).
    """
    if not isinstance(args_summary, str) or not args_summary:
        return None
    for part in args_summary.split(", "):
        field, sep, value = part.partition("=")
        if sep and field.strip() == key and value.strip():
            return value.strip()
    return None


def _status_glyph(status: str, palette: Palette) -> tuple[str, str]:
    """Map a lifecycle ``status`` to ``(glyph, style)`` via ICONS + the palette."""
    glyph = ICONS.agent_state.get(status, ICONS.tool_pending)
    if status in _FAIL_STATES:
        return glyph, f"{palette.error}"
    if status == "completed":
        return glyph, f"{palette.success}"
    if status == "running":
        return glyph, f"{palette.accent}"
    return glyph, f"{palette.warning}"


def _card_header(glyph: str, glyph_style: str, label: str, palette: Palette) -> Text:
    """Build the shared ``<glyph> <label>`` card header (matches the tool cards).

    Mirrors ``transcript_render._header``: identity/state lives in the glyph
    colour, the label is plain ``palette.assistant`` (no bold) per the design-system
    rule that font weight is never a state channel.
    """
    head = Text()
    head.append(f"{glyph} ", style=glyph_style)
    head.append(label, style=f"{palette.assistant}")
    return head


# ---------------------------------------------------------------------------
# spawn_agent / spawn_agents — the agent-launch card
# ---------------------------------------------------------------------------


def _render_spawn(payload: dict[str, Any], palette: Palette) -> RenderableType:
    """Compact agent-launch card: task (truncated), model, agent_type/label.

    Handles both the single ``spawn_agent`` (result ``{agent_id, status, task}``
    or an ``AgentResult`` dict) and the batch ``spawn_agents``
    (``{kind: "agent_batch", agents, spawned, rejected}``). Missing fields degrade
    to a one-liner rather than failing.
    """
    args_summary = payload.get("args_summary")
    model = _summary_value(args_summary, "model")
    agent_type = _summary_value(args_summary, "agent_type")
    info = _parse_result(payload.get("result"))

    # Batch fan-out → a summary header + one row per spawned task.
    if info is not None and info.get("kind") == "agent_batch":
        return _render_spawn_batch(info, model, agent_type, palette)

    status = str((info or {}).get("status") or "submitted")
    task = (info or {}).get("task") or _summary_value(args_summary, "task") or ""
    glyph, glyph_style = _status_glyph(status, palette)

    label = "spawn_agent"
    if agent_type:
        label = f"{label} · {agent_type}"
    head = _card_header(glyph, glyph_style, label, palette)
    head.append(f"  {status}", style=f"{palette.muted}")

    rows: list[RenderableType] = [head]
    if task:
        rows.append(Text(f"  {_truncate(task, _TASK_MAX)}", style=f"{palette.fg_base}"))
    meta = _spawn_meta(info or {}, model, palette)
    if meta is not None:
        rows.append(meta)
    return Group(*rows)


def _spawn_meta(info: dict[str, Any], model: str | None, palette: Palette) -> Text | None:
    """Build the dim ``model · agent <id>`` meta line, or ``None`` when empty."""
    bits: list[str] = []
    if model:
        bits.append(model)
    agent_id = info.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        bits.append(f"agent {agent_id[:8]}")
    if not bits:
        return None
    return Text("  " + "  ·  ".join(bits), style=f"{palette.muted}")


def _render_spawn_batch(
    info: dict[str, Any],
    model: str | None,
    agent_type: str | None,
    palette: Palette,
) -> RenderableType:
    """Batch ``spawn_agents`` card: header counts + per-task rows (capped)."""
    raw_agents = info.get("agents")
    agents: list[Any] = raw_agents if isinstance(raw_agents, list) else []
    spawned = info.get("spawned", len(agents))
    rejected = info.get("rejected", 0)

    label = "spawn_agents"
    if agent_type:
        label = f"{label} · {agent_type}"
    glyph_style = f"{palette.success}" if not rejected else f"{palette.warning}"
    head = _card_header(ICONS.agent_state.get("submitted", "⏳"), glyph_style, label, palette)
    head.append(f"  {spawned} spawned", style=f"{palette.muted}")
    if rejected:
        head.append(f", {rejected} rejected", style=f"{palette.warning}")
    if model:
        head.append(f"  ·  {model}", style=f"{palette.muted}")

    rows: list[RenderableType] = [head]
    for entry in agents[:_MAX_BATCH_ROWS]:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "submitted")
        glyph, gstyle = _status_glyph(status, palette)
        line = Text(f"  {glyph} ", style=gstyle)
        line.append(_truncate(entry.get("task") or "(task)", _DESC_MAX), style=f"{palette.fg_base}")
        rows.append(line)
    hidden = len(agents) - _MAX_BATCH_ROWS
    if hidden > 0:
        rows.append(Text(f"  … +{hidden} more", style=f"{palette.muted}"))
    return Group(*rows)


# ---------------------------------------------------------------------------
# check_agents — the fleet-status table
# ---------------------------------------------------------------------------


def _render_fleet(payload: dict[str, Any], palette: Palette) -> RenderableType:
    """Compact fleet-status table: agent · status · tokens/steps · last tool.

    Tokens are not carried on the ``check_agents`` payload today, so the metric
    column shows token totals when present (future-proof) and falls back to the
    always-present ``steps_completed`` otherwise.
    """
    info = _parse_result(payload.get("result"))
    agents = (info or {}).get("agents")
    if not isinstance(agents, list) or not agents:
        head = _card_header(ICONS.tool_pending, f"{palette.muted}", "check_agents", palette)
        head.append("  no agents", style=f"{palette.muted}")
        return head

    use_tokens = any(_agent_tokens(a) is not None for a in agents if isinstance(a, dict))
    metric_header = "tokens" if use_tokens else "steps"

    head = _card_header(ICONS.check, f"{palette.success}", "check_agents", palette)
    head.append(f"  {len(agents)} agent(s)", style=f"{palette.muted}")

    table = Table(box=None, show_edge=False, pad_edge=False, padding=(0, 2, 0, 0))
    for col in ("agent", "status", metric_header, "last tool"):
        table.add_column(col, style=f"{palette.muted}", header_style=f"dim {palette.muted}")

    for entry in agents:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        glyph, gstyle = _status_glyph(status, palette)
        agent_cell = Text(_agent_label(entry), style=f"{palette.fg_base}")
        status_cell = Text(f"{glyph} ", style=gstyle)
        status_cell.append(status or "—", style=f"{palette.fg_base}")
        tokens = _agent_tokens(entry)
        metric = str(tokens) if use_tokens and tokens is not None else str(
            entry.get("steps_completed", 0)
        )
        last_tool = str(entry.get("last_tool_id") or "—")
        table.add_row(agent_cell, status_cell, metric, last_tool)

    return Group(head, table)


def _agent_label(entry: dict[str, Any]) -> str:
    """Short agent identity for the table: ``<id8> task…`` (id or task, never raw)."""
    agent_id = entry.get("id")
    prefix = f"{agent_id[:8]} " if isinstance(agent_id, str) and agent_id else ""
    task = entry.get("task")
    return f"{prefix}{_truncate(task, 40)}".strip() if task else (prefix.strip() or "agent")


def _agent_tokens(entry: dict[str, Any]) -> int | None:
    """Return a token total for an agent row when the payload carries one."""
    total = entry.get("total_tokens") or entry.get("tokens")
    if isinstance(total, int):
        return total
    in_tok = entry.get("input_tokens")
    out_tok = entry.get("output_tokens")
    if isinstance(in_tok, int) or isinstance(out_tok, int):
        return int(in_tok or 0) + int(out_tok or 0)
    return None


# ---------------------------------------------------------------------------
# tool_search — the discovered-tools list
# ---------------------------------------------------------------------------

# Keep in sync with transcript_render._summarise_tool_search (same payload shape);
# kept local so this module stays self-contained from the render-path dispatch.
_FUNCTION_RE = re.compile(r"<function>\s*(\{.*?\})\s*</function>", re.DOTALL)


def _render_search(payload: dict[str, Any], palette: Palette) -> RenderableType:
    """Discovered-tools line list: ``name — one-line description`` per match."""
    raw = str(payload.get("result") or "")
    tools = _parse_search_tools(raw)
    if not tools:
        head = _card_header(ICONS.tool_pending, f"{palette.muted}", "tool_search", palette)
        head.append("  no tools loaded", style=f"{palette.muted}")
        return head

    head = _card_header(ICONS.check, f"{palette.success}", "tool_search", palette)
    head.append(f"  {len(tools)} tool(s) loaded", style=f"{palette.muted}")

    rows: list[RenderableType] = [head]
    for name, desc in tools[:_MAX_SEARCH_ROWS]:
        line = Text("  ")
        line.append(name, style=f"bold {palette.accent}")
        if desc:
            line.append(f"  — {_truncate(desc, _DESC_MAX)}", style=f"{palette.muted}")
        rows.append(line)
    hidden = len(tools) - _MAX_SEARCH_ROWS
    if hidden > 0:
        rows.append(Text(f"  … +{hidden} more", style=f"{palette.muted}"))
    return Group(*rows)


def _parse_search_tools(raw: str) -> list[tuple[str, str]]:
    """Extract ``(name, description)`` pairs from a ``<functions>…</functions>`` blob.

    Returns an empty list when ``raw`` is not such a payload. Never raises.
    """
    if "<function>" not in raw:
        return []
    tools: list[tuple[str, str]] = []
    for match in _FUNCTION_RE.finditer(raw):
        try:
            obj = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("name")
        if not isinstance(name, str) or not name:
            continue
        desc = obj.get("description")
        first_line = desc.splitlines()[0] if isinstance(desc, str) and desc else ""
        tools.append((name, first_line))
    return tools


# ---------------------------------------------------------------------------
# OrchestrationCards — atomic dispatcher
# ---------------------------------------------------------------------------


class OrchestrationCards:
    """Render the three orchestration tools as cards (one atomic class, palette DI).

    :meth:`render` returns a card renderable for an orchestration ``tool`` item,
    or ``None`` for any other tool so the caller can delegate to the base
    renderer. Never raises — a malformed payload degrades to a one-line header.
    """

    def __init__(self, palette: Palette = DEFAULT_PALETTE) -> None:
        """Bind the injected semantic palette (no globals)."""
        self._palette = palette

    def render(self, item: TranscriptItem) -> RenderableType | None:
        """Return a card for an orchestration tool item, else ``None``."""
        if item.kind != "tool":
            return None
        payload = dict(item.payload)
        tool_id = str(payload.get("tool_id", ""))
        if tool_id not in ORCHESTRATION_TOOL_IDS:
            return None
        try:
            if tool_id in _SPAWN_IDS:
                return _render_spawn(payload, self._palette)
            if tool_id in _FLEET_IDS:
                return _render_fleet(payload, self._palette)
            return _render_search(payload, self._palette)
        except Exception as exc:  # noqa: BLE001 — a card must never break the transcript
            return Text(f"[orchestration card error: {exc}]", style=f"{self._palette.error}")


def register_orchestration_cards(
    registry: MessageRendererRegistry,
    *,
    palette: Palette = DEFAULT_PALETTE,
) -> None:
    """Install the orchestration cards as a wrapping ``"tool"`` renderer.

    The wrapper draws a card for ``spawn_agent`` / ``spawn_agents`` /
    ``check_agents`` / ``tool_search`` and delegates every other tool to the
    canonical base tool renderer. The base is reached through a private staging
    registry (mirroring ``TranscriptView.on_mount``) so this registration is
    decoupled from order and never touches the render dispatch.

    Call this AFTER :func:`~mewbo_cli.tui.transcript_render.register_transcript_renderers`
    so the wrapper supersedes the plain ``"tool"`` renderer for these ids.

    Args:
        registry: The registry to install the wrapping renderer on.
        palette:  Injected semantic colour palette (DI — no globals).
    """
    cards = OrchestrationCards(palette)

    # Private staging registry holds the canonical base renderers; non-orchestration
    # tools route through it so we never depend on registration order.
    staging = MessageRendererRegistry()
    register_transcript_renderers(staging, palette=palette)

    def _render(item: TranscriptItem) -> RenderableType:
        card = cards.render(item)
        if card is not None:
            return card
        return staging.render(item)

    registry.register("tool", _render)


__all__ = [
    "ORCHESTRATION_TOOL_IDS",
    "OrchestrationCards",
    "register_orchestration_cards",
]
