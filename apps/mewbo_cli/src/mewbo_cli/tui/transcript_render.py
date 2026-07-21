#!/usr/bin/env python3
"""Transcript renderers, streaming cache, and helper classes.

This module provides the concrete rendering logic for the Mewbo TUI transcript.
It is wired into the transcript via the ``MessageRendererRegistry`` seam — the
``MewboApp`` never imports this directly.

Public surface (three things):

- :func:`register_transcript_renderers` — register all renderers on a
  ``MessageRendererRegistry``.  Called by the controller (``cli_master._run_app``)
  so the App stays closed.
- :class:`StreamingMarkdown` — stable-prefix streaming cache.  Keeps the
  rendered stable prefix and only re-renders the trailing partial on each
  delta.  Exposes ``_full_render_count`` as a test seam.
- :class:`ThinkingCollapser` / :class:`ToolOutputCollapser` — atomic helpers for
  the collapsible-thinking and truncated-output affordances.
"""

from __future__ import annotations

import ast
import difflib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any, Literal

from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

from mewbo_cli.aider_ui import render_markdown
from mewbo_cli.cli_diffview import unified_diff_to_old_new
from mewbo_cli.cli_icons import ICONS
from mewbo_cli.cli_theme import DEFAULT_PALETTE, Palette
from mewbo_cli.tui.seams import MessageRendererRegistry, TranscriptItem

# ---------------------------------------------------------------------------
# StreamingMarkdown — stable-prefix streaming cache (the flicker fix)
# ---------------------------------------------------------------------------
#
# Problem: re-rendering a 500-token markdown block on every streaming delta
# causes visible flicker — the whole block is torn down and rebuilt each time.
#
# Solution: split the accumulated text at the last "block boundary" (blank line
# or closing fence ````` ``` ````).  Everything BEFORE the boundary is the
# "stable prefix" — rendered once and cached.  Only the trailing partial (after
# the boundary) is re-rendered on each delta.  The stable prefix is promoted
# (becomes the new cache key) only when a new block boundary appears in the
# incoming text.
#
# Memoisation: render results are stored per (stable_prefix, width,
# theme_version).  A width or theme change invalidates the memo so the terminal
# layout is always correct.  A feed that is NOT a prefix-extension of the
# previous text (e.g., an out-of-order or reset event) triggers a full
# re-render of the stable prefix.


class StreamingMarkdown:
    """Stable-prefix streaming cache for incremental markdown rendering.

    Attributes:
        _full_render_count: Incremented every time the stable-prefix cache is
            **cold** (i.e. a full re-render of the stable prefix occurs).
            Exposed as a test seam so callers can assert that mid-block deltas
            do NOT trigger needless full re-renders.
    """

    def __init__(self) -> None:
        """Initialise with an empty buffer and a cold cache."""
        self._text: str = ""

        # The stable prefix is the substring up to the last block boundary.
        # Anything after that boundary is the "trailing partial".
        self._stable_prefix: str = ""

        # Memoised render for the stable prefix keyed by (prefix, width, theme_version).
        self._memo: tuple[str, int, int, RenderableType] | None = None

        # test seam: count full renders of the stable prefix
        self._full_render_count: int = 0

        # theme_version is incremented by the caller when the palette changes
        self._theme_version: int = 0

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    @property
    def text(self) -> str:
        """The full accumulated text (stable prefix + trailing partial)."""
        return self._text

    def feed(self, delta: str) -> None:
        """Append ``delta`` to the accumulated buffer and advance the stable prefix.

        If ``delta`` introduces a new blank-line or fence boundary, the stable
        prefix is extended to include the newly completed block(s).  The memo
        cache is invalidated only when the stable prefix actually changes.
        """
        new_text = self._text + delta

        # Detect new block boundaries in the appended text.
        new_stable = _find_stable_prefix(new_text)

        if new_stable != self._stable_prefix:
            # The stable prefix grew — the cached render is stale.
            self._stable_prefix = new_stable
            self._memo = None  # invalidate

        self._text = new_text

    def reset(self) -> None:
        """Reset the buffer and all caches (e.g. after a theme change or reuse)."""
        self._text = ""
        self._stable_prefix = ""
        self._memo = None
        self._full_render_count = 0

    def invalidate_theme(self) -> None:
        """Bump the theme version so the next render() rebuilds the stable part."""
        self._theme_version += 1
        self._memo = None

    def render(self, *, width: int = 80) -> tuple[RenderableType, RenderableType | None]:
        """Return ``(stable_renderable, partial_renderable)``.

        ``stable_renderable`` is the memoised render of the stable prefix.
        ``partial_renderable`` is a live render of the trailing partial text
        (None if the trailing partial is empty).

        The caller stacks the two renderables in a ``Group`` to display them.
        """
        # Stable prefix rendering (memoised per prefix+width+theme_version).
        stable_renderable = self._render_stable(width)

        # Trailing partial
        trailing = self._text[len(self._stable_prefix):]
        partial_renderable: RenderableType | None = None
        if trailing.strip():
            partial_renderable = render_markdown(trailing)

        return stable_renderable, partial_renderable

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _render_stable(self, width: int) -> RenderableType:
        """Return a memoised render of the stable prefix."""
        prefix = self._stable_prefix
        if not prefix.strip():
            return Text("")

        # Cache hit: same prefix, same width, same theme version.
        if self._memo is not None:
            cached_prefix, cached_width, cached_theme, cached_renderable = self._memo
            if (
                cached_prefix == prefix
                and cached_width == width
                and cached_theme == self._theme_version
            ):
                return cached_renderable

        # Cache miss → full re-render.
        self._full_render_count += 1
        renderable = render_markdown(prefix)
        self._memo = (prefix, width, self._theme_version, renderable)
        return renderable


def _find_stable_prefix(text: str) -> str:
    r"""Return the longest prefix of ``text`` that ends on a block boundary.

    A block boundary is a blank line (``\n\n``) or the close of a fenced
    code block (triple backtick).  The prefix includes the boundary itself
    so it is self-contained markdown.
    """
    # Walk backwards to find the last block boundary.
    # We look for the LAST occurrence so the stable prefix is maximal.
    fence = "```"
    last_boundary = -1

    # Scan for blank lines (paragraph boundaries)
    idx = text.rfind("\n\n")
    if idx != -1:
        last_boundary = max(last_boundary, idx + 2)  # include the blank line

    # Scan for closing fences (must have an opening fence before them)
    pos = 0
    while True:
        fence_idx = text.find(fence, pos)
        if fence_idx == -1:
            break
        # A closing fence ends a block; there must be an opening one before it.
        close = text.find(fence, fence_idx + len(fence))
        if close != -1:
            end = close + len(fence)
            # Advance past optional trailing newline
            if end < len(text) and text[end] == "\n":
                end += 1
            last_boundary = max(last_boundary, end)
            pos = close + len(fence)
        else:
            # Unmatched fence — don't treat as stable
            break

    if last_boundary == -1:
        return ""
    return text[:last_boundary]


# ---------------------------------------------------------------------------
# ThinkingCollapser — collapsible thinking block (3 modes)
# ---------------------------------------------------------------------------


class ThinkingCollapser:
    """Collapsible thinking block with 3 display modes.

    Modes cycle: ``collapsed`` → ``tail_window`` → ``expanded`` → ``collapsed``.

    Args:
        text:       Full thinking text.
        tail_lines: Number of tail lines shown in ``tail_window`` mode (default 5).
    """

    _MODES: tuple[str, ...] = ("collapsed", "tail_window", "expanded")

    def __init__(self, text: str, *, tail_lines: int = 5) -> None:
        """Initialise in collapsed mode."""
        self._text = text
        self._tail_lines = tail_lines
        self._mode_idx: int = 0

    @property
    def mode(self) -> Literal["collapsed", "tail_window", "expanded"]:
        """Current display mode."""
        return self._MODES[self._mode_idx]  # type: ignore[return-value]

    def cycle(self) -> None:
        """Advance to the next mode."""
        self._mode_idx = (self._mode_idx + 1) % len(self._MODES)

    def visible_text(self) -> str:
        """Return the visible text for the current mode."""
        if self.mode == "collapsed":
            return "(thinking collapsed — click to expand)"
        if self.mode == "tail_window":
            lines = self._text.splitlines()
            tail = lines[-self._tail_lines:]
            return "\n".join(tail)
        # expanded
        return self._text

    def as_renderable(self, *, palette: Palette) -> RenderableType:
        """Render the thinking block for the current mode."""
        mode = self.mode
        if mode == "collapsed":
            return Text(
                f"💭 Thinking… ({len(self._text.splitlines())} lines)",
                style=f"{palette.muted}",
            )
        if mode == "tail_window":
            header = Text(
                f"💭 Thinking (last {self._tail_lines} lines — click to expand):",
                style=f"{palette.muted}",
            )
            body = Text(self.visible_text(), style=f"{palette.muted}")
            return Group(header, body)
        # expanded
        header = Text("💭 Thinking:", style=f"{palette.muted}")
        body = Text(self._text, style=f"{palette.muted}")
        return Group(header, body)


# ---------------------------------------------------------------------------
# ToolOutputCollapser — truncated tool output with expand affordance
# ---------------------------------------------------------------------------


class ToolOutputCollapser:
    """Truncated tool output with an expand affordance.

    When the output exceeds ``max_lines``, ``visible_text()`` returns the
    first ``max_lines`` lines followed by an "N lines hidden — expand"
    hint.  Calling :meth:`expand` reveals the full output.

    Args:
        text:       Raw tool output text.
        max_lines:  Lines to show before truncating (default 20).
    """

    def __init__(self, text: str, *, max_lines: int = 20) -> None:
        """Initialise with the raw output and a line cap."""
        self._text = text
        self._max_lines = max_lines
        self._expanded: bool = False

    @property
    def is_truncated(self) -> bool:
        """True when truncation is active (not expanded and output exceeds cap)."""
        if self._expanded:
            return False
        lines = self._text.splitlines()
        return len(lines) > self._max_lines

    def expand(self) -> None:
        """Reveal the full output (disable truncation)."""
        self._expanded = True

    def collapse(self) -> None:
        """Re-enable truncation."""
        self._expanded = False

    def visible_text(self) -> str:
        """Return the visible portion of the output."""
        if not self.is_truncated:
            return self._text
        lines = self._text.splitlines()
        visible = lines[: self._max_lines]
        hidden = len(lines) - self._max_lines
        visible.append(f"… {hidden} lines hidden — expand to see all")
        return "\n".join(visible)


# ---------------------------------------------------------------------------
# Diffstat helper
# ---------------------------------------------------------------------------


def _diffstat(old_text: str, new_text: str) -> str:
    """Return a compact ``+N -M`` diffstat string for old→new text.

    Counts added and deleted lines using a simple set-difference approach
    that is fast and allocation-light for typical file edits.
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)
    added = deleted = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(
        None, old_lines, new_lines, autojunk=False
    ).get_opcodes():
        if tag in ("replace", "delete"):
            deleted += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return f"+{added} -{deleted}"


# ---------------------------------------------------------------------------
# Per-kind renderers
# ---------------------------------------------------------------------------

# Tool names that get Bash-style rendering (show command, then output).
_BASH_TOOL_IDS = frozenset({"bash", "shell", "run_shell_command", "execute_bash"})

# Name hints (substring, case-insensitive) that also denote a shell/exec tool, so
# vendored ids like ``aider_shell_tool`` are recognised as shell executions.
_BASH_TOOL_HINTS = ("shell", "bash", "terminal")


def is_shell_tool(tool_id: str) -> bool:
    """True when ``tool_id`` denotes a shell/bash execution (exact id or name hint)."""
    lower = tool_id.lower()
    return tool_id in _BASH_TOOL_IDS or any(hint in lower for hint in _BASH_TOOL_HINTS)

# Tool names that get Edit/Write-style rendering (diffstat + optional DiffView mount).
_EDIT_TOOL_IDS = frozenset({"Edit", "Write", "edit", "write", "str_replace_editor"})

# Substrings that flag a read/view tool (matched case-insensitively on tool_id).
_READ_TOOL_HINTS = ("read", "view", "cat", "list_dir")

# Body output truncates to this many lines before showing a "… +N lines" hint.
_BODY_MAX_LINES = 15

# Tokens that mark a result string as an error (drives the ✗ glyph).
_ERROR_HINTS = ("error", "traceback", "exception", "failed", "fatal")


def _result_is_error(result: object) -> bool:
    """Heuristically decide whether a tool result looks like an error."""
    if result is None:
        return False
    text = str(result).lstrip().lower()
    return any(text.startswith(h) or f" {h}" in text[:80] for h in _ERROR_HINTS)


def _elapsed_suffix(payload: Mapping[str, Any]) -> str:
    """Return a ``· {N}s`` elapsed suffix for a settled tool card, else ``""``.

    The mutable tool card carries ``elapsed`` once it settles; the running
    card has none, so this is empty while in flight and appears only on the
    settled (muted) line — e.g. ``✓ ran bash · 1.2s``.
    """
    elapsed = payload.get("elapsed")
    if isinstance(elapsed, (int, float)):
        return f" · {float(elapsed):.1f}s"
    return ""


def _pretty_json(raw: str) -> str:
    """Re-dump a JSON string with indent=2; return ``raw`` unchanged on failure.

    Only attempts a parse when the stripped text starts with ``{`` or ``[`` so
    a plain output string is never mangled.  Never raises.
    """
    stripped = raw.strip()
    if not stripped or stripped[0] not in "{[":
        return raw
    try:
        parsed: Any = json.loads(stripped)
    except (ValueError, TypeError):
        return raw
    try:
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (ValueError, TypeError):
        return raw


def _unwrap_content_blocks(value: object) -> str | None:
    """Concatenate the ``text`` of an MCP content-block list, else ``None``.

    An MCP tool result often comes back as a list of content blocks shaped like
    ``[{"type": "text", "text": "…"}, …]`` (the Python ``repr`` of which we may
    also receive as a string).  Surface just the joined text; return ``None``
    when ``value`` is not such a list so the caller can fall through.
    """
    if not isinstance(value, list) or not value:
        return None
    texts: list[str] = []
    for block in value:
        if not isinstance(block, dict):
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            texts.append(block["text"])
        else:
            # Mixed / unknown block kinds → don't claim to unwrap it.
            return None
    return "\n".join(texts) if texts else None


def _summarise_tool_search(raw: str) -> str | None:
    r"""Collapse a ``<functions>…</functions>`` tool_search payload to one line.

    ``tool_search`` returns the full JSON schema of each loaded tool wrapped in
    ``<function>{…}</function>`` blocks — raw noise in the transcript.  Extract
    the tool ``name``\ s and return a compact ``N tools loaded: a, b, …`` summary
    (capped), or ``None`` when ``raw`` is not such a payload.
    """
    if "<functions>" not in raw and "<function>" not in raw:
        return None
    names: list[str] = []
    for match in re.finditer(r"<function>\s*(\{.*?\})\s*</function>", raw, re.DOTALL):
        try:
            obj = json.loads(match.group(1))
        except (ValueError, TypeError):
            continue
        name = obj.get("name") if isinstance(obj, dict) else None
        if isinstance(name, str) and name:
            names.append(name)
    if not names:
        return None
    shown = names[:8]
    suffix = f", … +{len(names) - len(shown)} more" if len(names) > len(shown) else ""
    return f"{len(names)} tools loaded: {', '.join(shown)}{suffix}"


def _safe_literal_eval(text: str) -> object:
    """``ast.literal_eval`` that returns ``None`` instead of raising.

    Used to recover an MCP content-block list from the Python ``repr`` we receive
    when the result was ``str()``'d (single-quoted dict keys are not valid JSON).
    """
    try:
        return ast.literal_eval(text)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None


def _extract_meaningful(raw: str) -> str:
    """Unwrap a Mewbo tool result envelope to its human-meaningful content.

    Mewbo's built-in tools return a JSON envelope (``{"kind": "shell"|"file"|
    "dir", …}``).  Showing the raw envelope is noise — surface the part a reader
    actually wants: shell → ``stdout`` (+ ``stderr`` / non-zero exit), file →
    ``text``, dir → ``entries``.  MCP results that arrive as a list of
    ``{"type":"text","text":…}`` content blocks collapse to their joined text,
    and a ``<functions>…</functions>`` tool_search payload collapses to a
    one-line summary of the loaded tool names.  Unknown shapes fall back to
    pretty-printed JSON (so MCP/other tools still read cleanly).  Never raises.
    """
    # tool_search schema dump → concise one-line summary.
    summary = _summarise_tool_search(raw)
    if summary is not None:
        return summary

    stripped = raw.strip()
    if not stripped or stripped[0] != "{":
        # MCP content-block lists arrive either as the live Python object (str()'d
        # to a repr) or as JSON text — try both before pretty-printing.
        if stripped and stripped[0] == "[":
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = _safe_literal_eval(stripped)
            blocks = _unwrap_content_blocks(parsed)
            if blocks is not None:
                return blocks
        # Arrays / non-object JSON / plain text → pretty-print if JSON, else verbatim.
        return _pretty_json(raw)
    try:
        obj = json.loads(stripped)
    except (ValueError, TypeError):
        return raw
    if not isinstance(obj, dict):
        return _pretty_json(stripped)
    kind = obj.get("kind")
    if kind == "shell":
        parts: list[str] = []
        out = str(obj.get("stdout", "") or "").rstrip("\n")
        err = str(obj.get("stderr", "") or "").rstrip("\n")
        if out:
            parts.append(out)
        if err:
            parts.append(f"[stderr] {err}")
        code = obj.get("exit_code")
        if isinstance(code, int) and code != 0:
            parts.append(f"[exit {code}]")
        return "\n".join(parts) if parts else "(no output)"
    if kind == "file" and isinstance(obj.get("text"), str):
        return obj["text"]
    if kind == "dir" and isinstance(obj.get("entries"), list):
        return "\n".join(str(e) for e in obj["entries"])
    if kind == "diff" and isinstance(obj.get("text"), str):
        # A diff envelope is mounted as a colored DiffView by the transcript;
        # this textual fallback (plain REPL, error path) shows the raw diff body.
        return obj["text"]
    return _pretty_json(stripped)


def _diff_envelope(result: object) -> tuple[str, str | None] | None:
    """Return ``(unified_diff_text, file_path)`` when ``result`` is a diff envelope.

    The ``file_edit_tool`` returns ``{"kind": "diff", "text": "<unified diff>",
    "files": [...]}``.  When ``result`` (a JSON string) matches that shape,
    surface the diff text + first file path so the caller can mount a colored
    :class:`~mewbo_cli.cli_diffview.DiffView`.  Returns ``None`` otherwise.  Never
    raises.
    """
    raw = str(result).strip()
    if not raw.startswith("{") or '"kind"' not in raw:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or obj.get("kind") != "diff":
        return None
    text = obj.get("text")
    if not isinstance(text, str) or not text:
        return None
    files = obj.get("files")
    file_path = files[0] if isinstance(files, list) and files else None
    return text, (str(file_path) if file_path is not None else None)


def _truncated_body(raw: str) -> Text:
    """Return a dim, truncated body Text with a ``… +N lines`` hint.

    Unwraps the Mewbo result envelope to its meaningful content (pretty-printing
    unknown JSON), then caps at :data:`_BODY_MAX_LINES` lines.
    """
    content = _extract_meaningful(raw)
    lines = content.splitlines()
    if len(lines) > _BODY_MAX_LINES:
        hidden = len(lines) - _BODY_MAX_LINES
        shown = lines[:_BODY_MAX_LINES]
        shown.append(f"… +{hidden} lines")
        content = "\n".join(shown)
    return Text(content, style="dim")


def _make_user_renderer(palette: Palette) -> Callable[[TranscriptItem], RenderableType]:
    """Return a user-turn renderer."""

    def _render(item: TranscriptItem) -> RenderableType:
        # Role identity = colour. The user turn is the user-colour prompt glyph +
        # the message in the user colour (the .t-user left rail reinforces it),
        # so it never blends into the assistant's default-foreground markdown.
        text = Text()
        text.append("❯ ", style=f"bold {palette.user}")
        agent_label = item.payload.get("agent_label")
        if agent_label:
            text.append(f"[{agent_label}] ", style=f"dim {palette.user}")
        text.append(str(item.payload.get("text", "")), style=f"{palette.user}")
        return text

    return _render


def _make_assistant_renderer(palette: Palette) -> Callable[[TranscriptItem], RenderableType]:
    """Return an assistant-turn renderer using the stable-prefix streaming path."""

    def _render(item: TranscriptItem) -> RenderableType:
        try:
            text = str(item.payload.get("text", ""))
            agent_label = item.payload.get("agent_label")

            # Collapsible thinking section
            thinking = item.payload.get("thinking")
            parts: list[RenderableType] = []

            if thinking:
                tc = ThinkingCollapser(str(thinking))
                parts.append(tc.as_renderable(palette=palette))
                duration = item.payload.get("thinking_duration")
                if isinstance(duration, (int, float)):
                    parts.append(
                        Text(f"  ⤷ Thought for {float(duration):.1f}s", style=f"{palette.muted}")
                    )

            if agent_label:
                label_text = Text(
                    f"  [{agent_label}]", style=f"dim italic {palette.assistant}"
                )
                parts.append(label_text)

            if text.strip():
                parts.append(render_markdown(text))

            return Group(*parts) if parts else Text("")
        except Exception as exc:  # noqa: BLE001
            return Text(f"[render error: {exc}]", style=f"{palette.error}")

    return _render


def _make_tool_renderer(palette: Palette) -> Callable[[TranscriptItem], RenderableType]:
    """Return a tool-call renderer keyed by ``tool_id``."""

    def _render(item: TranscriptItem) -> RenderableType:
        try:
            payload = item.payload
            tool_id = str(payload.get("tool_id", "tool"))
            operation = payload.get("operation", "")
            result = payload.get("result")
            is_mcp = payload.get("is_mcp", False)
            agent_label = payload.get("agent_label")

            # Label: [agent] tool_id:operation (MCP)
            label = tool_id
            if operation:
                label = f"{label}:{operation}"
            if is_mcp:
                label = f"{label} (MCP)"
            if agent_label:
                label = f"[{agent_label}] {label}"

            glyph, glyph_style = _status_glyph(result, palette)
            lower_id = tool_id.lower()
            label = f"{label}{_elapsed_suffix(payload)}"

            if is_shell_tool(tool_id):
                return _render_bash_tool(payload, palette, glyph, glyph_style)
            if tool_id in _EDIT_TOOL_IDS:
                # DiffView mounting is handled by TranscriptView.write_item when
                # old_text and new_text are present; here we emit the static renderable.
                return _render_edit_tool(label, payload, palette, glyph, glyph_style)
            if any(hint in lower_id for hint in _READ_TOOL_HINTS):
                return _render_read_tool(label, payload, palette, glyph, glyph_style)
            return _render_default_tool(label, payload, palette, glyph, glyph_style)
        except Exception as exc:  # noqa: BLE001
            return Text(f"[tool render error: {exc}]", style=f"{palette.error}")

    return _render


def _status_glyph(result: object, palette: Palette) -> tuple[str, str]:
    """Return ``(glyph, style)`` for a tool header from its result state.

    - running / pending (``result is None``) → ``●`` in the accent colour.
    - error-looking result → ``✗`` in the error colour.
    - otherwise (success) → ``✓`` in the success colour.
    """
    if result is None:
        return "●", f"{palette.accent}"
    if _result_is_error(result):
        return ICONS.error, f"{palette.error}"
    return ICONS.check, f"{palette.success}"


def _header(glyph: str, glyph_style: str, label: str, palette: Palette) -> Text:
    """Build a ``<glyph> <label>`` header line; glyph carries its own style."""
    head = Text()
    head.append(f"{glyph} ", style=glyph_style)
    head.append(label, style=f"{palette.assistant}")
    return head


def _render_bash_tool(
    payload,  # noqa: ANN001
    palette: Palette,
    glyph: str,
    glyph_style: str,
) -> RenderableType:
    """Shell execution: an unmistakable ``$ <command>`` prompt + terminal output.

    The ``$`` prompt is rendered in the success colour and the command in bold so
    the block reads as a shell execution at a glance; the ``.t-bash`` rail (a
    distinct colour from generic tool cards) reinforces it. Output is the payload,
    so it stays legible (not dimmed away), truncated with a ``… +N lines`` hint.
    """
    command = payload.get("command") or payload.get("args_summary") or ""
    head = Text()
    head.append(f"{glyph} ", style=glyph_style)
    head.append("$ ", style=f"bold {palette.success}")
    head.append(str(command).rstrip(), style=f"bold {palette.fg_base}")
    suffix = _elapsed_suffix(payload)
    if suffix:
        head.append(suffix, style=f"{palette.muted}")

    result = payload.get("result")
    if result is None:
        return head
    body = _extract_meaningful(str(result))
    lines = body.splitlines()
    if len(lines) > _BODY_MAX_LINES:
        hidden = len(lines) - _BODY_MAX_LINES
        body = "\n".join(lines[:_BODY_MAX_LINES] + [f"… +{hidden} lines"])
    return Group(head, Text(body, style=f"{palette.fg_base}"))


def _render_edit_tool(
    label: str,
    payload,  # noqa: ANN001
    palette: Palette,
    glyph: str,
    glyph_style: str,
) -> RenderableType:
    """Edit/Write tool: ``<glyph> <label> <file_path>  +A -D`` header.

    When old_text + new_text are both present in the payload, the caller
    (TranscriptView.write_item) will mount a DiffView child widget in addition
    to this static renderable.
    """
    old_text = payload.get("old_text")
    new_text = payload.get("new_text")
    file_path = payload.get("file_path", "")

    head = Text()
    head.append(f"{glyph} ", style=glyph_style)
    line = label
    if file_path:
        line = f"{line} {file_path}"
    if old_text is not None and new_text is not None:
        line = f"{line}  {_diffstat(str(old_text), str(new_text))}"
    head.append(line, style=f"{palette.assistant}")
    return head


def _render_read_tool(
    label: str,
    payload,  # noqa: ANN001
    palette: Palette,
    glyph: str,
    glyph_style: str,
) -> RenderableType:
    """Read/view tool: COMPACT one-liner (header + size hint), no body.

    Reads are the agent's most frequent tool; dumping each file body would bloat
    the viewport. We show only ``<glyph> <label> <path>  · N lines`` — the
    content is available in the file itself, not worth re-printing inline.
    """
    path = payload.get("file_path") or payload.get("path") or ""
    head_label = f"{label} {path}".rstrip() if path else label
    head = _header(glyph, glyph_style, head_label, palette)
    summary = _read_summary(payload)
    if summary:
        head.append(f"  · {summary}", style="dim")
    return head


def _read_summary(payload) -> str:  # noqa: ANN001
    """Return a compact size hint for a read result (``N lines``), never raising."""
    result = payload.get("result")
    if result is None:
        return "reading…"
    raw = str(result).strip()
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            text = obj.get("text")
            if isinstance(text, str):
                return f"{len(text.splitlines())} lines"
            total = obj.get("total_lines")
            if isinstance(total, int):
                return f"{total} lines"
    return f"{len(raw.splitlines())} lines"


def _render_default_tool(
    label: str,
    payload,  # noqa: ANN001
    palette: Palette,
    glyph: str,
    glyph_style: str,
) -> RenderableType:
    """Generic/MCP tool: header + pretty-printed, truncated (JSON-aware) body.

    A ``kind: diff`` result envelope (e.g. ``file_edit_tool``) shows a diffstat
    header only — the colored diff itself is a ``DiffView`` child mounted by
    ``TranscriptView.write_item``, so the JSON body is suppressed here.
    """
    head = _header(glyph, glyph_style, label, palette)
    args_summary = payload.get("args_summary")
    if args_summary:
        head.append(f"  {args_summary}", style="dim")

    result = payload.get("result")
    if result is None:
        return head

    envelope = _diff_envelope(result)
    if envelope is not None:
        diff_text, file_path = envelope
        old_text, new_text = unified_diff_to_old_new(diff_text)
        if file_path:
            head.append(f"  {file_path}", style="dim")
        head.append(f"  {_diffstat(old_text, new_text)}", style="dim")
        return head

    return Group(head, _truncated_body(str(result)))


# A proposed plan card caps its inline body at this many source lines so a long
# plan never blows out the transcript viewport; the full plan stays reviewable in
# the scrollable approval modal (and in plan.md).
_PLAN_PREVIEW_MAX_LINES = 40


def _plan_approval_footer(palette: Palette) -> Text:
    """Build the inline Approve / Keep-planning / Reject decision line.

    This is the visible label for THIS card's decision; the keys mirror the
    plan-approval modal (``a``/``k``/``r``) that pops over it so the affordance
    reads as one cohesive decision surface. Color = action identity
    (success=approve, accent=keep-planning, error=reject); the key + verb for
    each recede via ``palette.muted`` so the actions read first.
    """
    line = Text()
    line.append("Decision:  ", style=f"{palette.muted}")
    line.append(f"{ICONS.check} [a] Approve & execute", style=f"bold {palette.success}")
    line.append("     ", style=f"{palette.muted}")
    line.append(f"{ICONS.refine} [k] Keep planning", style=f"bold {palette.accent}")
    line.append("     ", style=f"{palette.muted}")
    line.append(f"{ICONS.cross} [r] Reject", style=f"bold {palette.error}")
    return line


def _plan_body(text: str, palette: Palette) -> RenderableType:
    """Render the plan markdown, capping an over-long plan with a muted hint.

    Full markdown fidelity flows through the shared ``render_markdown`` path
    (headings/bullets/code/bold/tables). When the plan exceeds
    :data:`_PLAN_PREVIEW_MAX_LINES` source lines the inline card shows the head
    plus a ``… +N more lines`` hint — the full plan is reviewable in the
    scrollable approval modal.
    """
    if not text:
        return Text("(empty plan)", style=f"{palette.muted}")
    lines = text.splitlines()
    if len(lines) <= _PLAN_PREVIEW_MAX_LINES:
        return render_markdown(text)
    hidden = len(lines) - _PLAN_PREVIEW_MAX_LINES
    head = "\n".join(lines[:_PLAN_PREVIEW_MAX_LINES])
    return Group(
        render_markdown(head),
        Text(
            f"… +{hidden} more lines — review the full plan in the approval prompt",
            style=f"{palette.muted}",
        ),
    )


def _make_plan_proposal_renderer(
    palette: Palette,
) -> Callable[[TranscriptItem], RenderableType]:
    """Return a renderer that draws the proposed plan as a clean bordered card.

    The plan body is the ``plan.md`` markdown (rendered via the one
    ``render_markdown`` path), wrapped in a bordered "Proposed plan" Panel with
    an inline Approve/Keep-planning/Reject decision footer — never the planner's
    raw spawn/check tool JSON. The ``.t-plan_proposal`` accent rail (transcript
    CSS) reinforces it; long plans cap to a bounded preview.
    """

    def _render(item: TranscriptItem) -> RenderableType:
        try:
            text = str(item.payload.get("text", "")).strip()
            revision = item.payload.get("revision")
            title = Text()
            title.append(f"{ICONS.plan} Proposed plan", style=f"bold {palette.accent}")
            if isinstance(revision, int) and revision:
                title.append(f"  ·  revision {revision}", style=f"{palette.muted}")
            return Panel(
                Group(_plan_body(text, palette), Text(""), _plan_approval_footer(palette)),
                title=title,
                title_align="left",
                border_style=f"{palette.accent}",
                padding=(0, 1),
            )
        except Exception as exc:  # noqa: BLE001
            return Text(f"[plan render error: {exc}]", style=f"{palette.error}")

    return _render


def _make_error_renderer(palette: Palette) -> Callable[[TranscriptItem], RenderableType]:
    """Return an error-kind renderer styled with palette.error."""

    def _render(item: TranscriptItem) -> RenderableType:
        try:
            text = str(item.payload.get("text", "Unknown error"))
            err_text = Text(f"{ICONS.error} {text}", style=f"{palette.error}")
            return err_text
        except Exception as exc:  # noqa: BLE001
            return Text(f"[error render error: {exc}]")

    return _render


# ---------------------------------------------------------------------------
# DiffView mount helper (consumed by TranscriptView.write_item)
# ---------------------------------------------------------------------------


def diff_envelope_for(payload: Mapping[str, Any]) -> tuple[str, str | None] | None:
    """Return ``(unified_diff_text, file_path)`` when a tool payload is a diff.

    Public seam over :func:`_diff_envelope` so ``TranscriptView.write_item`` can
    mount a colored ``DiffView`` for a ``kind: diff`` result envelope (e.g.
    ``file_edit_tool``) without re-parsing JSON itself.  Returns ``None`` when the
    payload's ``result`` is not a diff envelope.
    """
    result = payload.get("result")
    if result is None:
        return None
    return _diff_envelope(result)


# ---------------------------------------------------------------------------
# Public registration function
# ---------------------------------------------------------------------------


def register_transcript_renderers(
    registry: MessageRendererRegistry,
    *,
    palette: Palette = DEFAULT_PALETTE,
) -> None:
    """Register all transcript renderers on ``registry``.

    Registered kinds:
    - ``user``      — bold user-colour prompt prefix + text.
    - ``assistant`` — markdown via ``render_markdown``; thinking block when
                      ``payload["thinking"]`` is present; agent label prefix
                      for sub-agent output.
    - ``tool``      — keyed by ``tool_id``: Bash (command+output),
                      Edit/Write (diffstat + DiffView hook), generic (bullet tree).
    - ``plan_proposal`` — the ``plan.md`` markdown as a bordered "Proposed plan"
                      card with an Approve/Refine/Reject footer.
    - ``error``     — palette.error styled line.

    Respects the existing "do not clobber pre-registered kinds" convention used
    by ``MewboApp.on_mount``.  Callers that want to override a specific kind
    should register AFTER calling this function.

    Args:
        registry: The :class:`~mewbo_cli.tui.seams.MessageRendererRegistry`
            to register into.
        palette:  Injected semantic colour palette (DI — no globals).
    """
    registry.register("user", _make_user_renderer(palette))
    registry.register("assistant", _make_assistant_renderer(palette))
    registry.register("tool", _make_tool_renderer(palette))
    registry.register("plan_proposal", _make_plan_proposal_renderer(palette))
    registry.register("error", _make_error_renderer(palette))


__all__ = [
    "StreamingMarkdown",
    "ThinkingCollapser",
    "ToolOutputCollapser",
    "diff_envelope_for",
    "register_transcript_renderers",
]
