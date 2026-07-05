"""DiffView — reusable Textual diff widget for the Mewbo CLI (task #153).

A single :class:`DiffView` widget is mounted by both the approval modal (#154)
and the transcript tool-output (#152).  It owns:

- difflib-based hunk computation (``DiffLine`` typed model)
- Rich ``Syntax`` highlighting with hash-based cache (no recompute on resize)
- Width-adaptive split/unified layout (``auto`` threshold = ``split_min_width``)
- Honest truncation of long equal runs with expand toggle
- Horizontal scroll with leading/trailing ``…`` truncation markers
- All colours sourced from the injected ``Palette`` (no hardcoded hex)

Usage::

    from mewbo_cli.cli_diffview import DiffView
    from mewbo_cli.cli_theme import DEFAULT_PALETTE

    widget = DiffView(old_text, new_text, palette=DEFAULT_PALETTE, file_path="app.py")
"""

from __future__ import annotations

import difflib
import hashlib
from dataclasses import dataclass
from typing import Literal

from rich.containers import Lines
from rich.syntax import Syntax
from rich.text import Text
from textual.binding import Binding
from textual.widget import Widget

from mewbo_cli.cli_theme import Palette

# ---------------------------------------------------------------------------
# DiffLine — typed line model
# ---------------------------------------------------------------------------


def unified_diff_to_old_new(diff_text: str) -> tuple[str, str]:
    """Reconstruct ``(old_text, new_text)`` from a unified-diff string.

    Parses the ``---``/``+++`` header and ``@@`` hunks, collecting context
    (``  ``), deletion (``-``) and addition (``+``) lines into the two sides.
    Lines outside any hunk (and the file/hunk headers themselves) are ignored.
    Tolerant of malformed input — unparseable lines are simply skipped — so it
    never raises; the result feeds straight into :class:`DiffView`.
    """
    old_lines: list[str] = []
    new_lines: list[str] = []
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            # Skip the ``---``/``+++`` (and any ``diff``/``index``) header rows.
            continue
        if line.startswith("\\"):
            # "\ No newline at end of file" marker — not content.
            continue
        if line.startswith("+"):
            new_lines.append(line[1:])
        elif line.startswith("-"):
            old_lines.append(line[1:])
        else:
            # Context line (leading space) or a bare line → present on both sides.
            body = line[1:] if line.startswith(" ") else line
            old_lines.append(body)
            new_lines.append(body)
    old = "\n".join(old_lines)
    new = "\n".join(new_lines)
    return old, new


@dataclass(frozen=True)
class DiffLine:
    """One visual row in a diff.

    Attributes:
        kind:   ``"add"`` / ``"del"`` / ``"eq"``
        old_no: 1-based line number on the old side; ``None`` for add lines.
        new_no: 1-based line number on the new side; ``None`` for del lines.
        text:   Raw text content (no newline).
    """

    kind: Literal["add", "del", "eq"]
    old_no: int | None
    new_no: int | None
    text: str


# ---------------------------------------------------------------------------
# DiffView widget
# ---------------------------------------------------------------------------


class DiffView(Widget):
    """Self-contained diff widget: split or unified, syntax highlighted, palette-driven.

    Constructor args:
        old_text:       Left-hand (old) content.
        new_text:       Right-hand (new) content.
        palette:        Injected semantic colour palette (DI, no globals).
        file_path:      Optional path used for lexer sniffing (e.g. ``"app.py"``).
        line_numbers:   Show gutter line numbers (default True).
        layout:         ``"auto"`` / ``"split"`` / ``"unified"``.
        split_min_width: Minimum content width for auto-split (default 120).
        context_lines:  Equal-run lines kept visible around a change (default 3).
        syntax_theme:   Rich syntax theme name (default ``"ansi_dark"``).
        name/id/classes: Textual widget identifiers.

    Public properties:
        palette:          Current :class:`~mewbo_cli.cli_theme.Palette`.
        diff_lines:       Full :class:`DiffLine` list (untruncated, unreduced).
        visible_rows:     Rendered rows as ``(kind, text, diffline)`` tuples (post-truncation).
        effective_layout: ``"split"`` or ``"unified"`` as currently resolved.
        expanded:         Whether equal-run collapse is disabled.
        _highlight_misses: Cache-miss counter (test seam).

    Public methods:
        set_palette(palette):  Re-theme and invalidate the highlight cache.
        expand_all():          Reveal all collapsed equal runs.
        collapse_all():        Re-collapse equal runs.
    """

    BINDINGS = [
        Binding("t", "toggle_layout", "Toggle layout", show=False),
        Binding("e", "toggle_expand", "Expand/collapse", show=False),
        Binding("enter", "toggle_expand", "Expand/collapse", show=False),
        Binding("left", "scroll_left", "Scroll left", show=False),
        Binding("right", "scroll_right", "Scroll right", show=False),
    ]

    # Allow keyboard focus so bindings fire
    can_focus = True

    # Amount to scroll horizontally on each key press (chars)
    _H_SCROLL_STEP = 8

    def __init__(
        self,
        old_text: str,
        new_text: str,
        *,
        palette: Palette,
        file_path: str | None = None,
        line_numbers: bool = True,
        layout: Literal["auto", "split", "unified"] = "auto",
        split_min_width: int = 120,
        context_lines: int = 3,
        syntax_theme: str = "ansi_dark",
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        """Initialise DiffView with old/new content and display options."""
        super().__init__(name=name, id=id, classes=classes)
        self._old_text = old_text
        self._new_text = new_text
        self._palette = palette
        self._file_path = file_path
        self._line_numbers = line_numbers
        self._layout_mode: Literal["auto", "split", "unified"] = layout
        self._split_min_width = split_min_width
        self._context_lines = context_lines
        self._syntax_theme = syntax_theme

        # Widget state
        self._expanded = False
        self._h_offset = 0  # horizontal scroll offset in chars
        # "forced" layout when user presses 't' (None = use _layout_mode)
        self._forced_layout: Literal["split", "unified"] | None = None

        # Computed diff (built once on first access)
        self._diff_lines: list[DiffLine] | None = None

        # Highlight cache: key → list[Text] per side ("old" / "new")
        self._highlight_cache: dict[str, Lines] = {}
        # Test seam: incremented every time a real highlight occurs
        self._highlight_misses: int = 0

        # Computed visible rows cache (invalidated on render)
        self._visible_rows: list[tuple[str, str, DiffLine | None]] | None = None

    @classmethod
    def from_unified_diff(
        cls,
        diff_text: str,
        *,
        palette: Palette,
        file_path: str | None = None,
        classes: str | None = None,
    ) -> DiffView:
        """Build a :class:`DiffView` from a unified-diff string.

        Reconstructs the old/new sides via :func:`unified_diff_to_old_new` so a
        tool that only emits a unified diff (e.g. ``file_edit_tool``) renders as
        a colored add/del diff through the same widget Edit/Write use — no
        bespoke diff code. ``file_path`` drives lexer sniffing when provided.
        """
        old_text, new_text = unified_diff_to_old_new(diff_text)
        return cls(
            old_text,
            new_text,
            palette=palette,
            file_path=file_path,
            classes=classes,
        )

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def palette(self) -> Palette:
        """Current semantic colour palette."""
        return self._palette

    @property
    def diff_lines(self) -> list[DiffLine]:
        """Full DiffLine list; computed once and cached."""
        if self._diff_lines is None:
            self._diff_lines = self._compute_diff()
        return self._diff_lines

    @property
    def visible_rows(self) -> list[tuple[str, str, DiffLine | None]]:
        """Visible rows as ``(kind, text, diffline)`` after truncation.

        ``kind`` is one of ``"add"`` / ``"del"`` / ``"eq"`` / ``"hidden"``.
        ``diffline`` is the originating :class:`DiffLine`; ``None`` for
        ``"hidden"`` placeholder rows.
        """
        if self._visible_rows is None:
            self._visible_rows = self._compute_visible_rows()
        return self._visible_rows

    @property
    def effective_layout(self) -> Literal["split", "unified"]:
        """Resolved layout: 'split' or 'unified'."""
        if self._forced_layout is not None:
            return self._forced_layout
        if self._layout_mode == "auto":
            return "split" if self.size.width > self._split_min_width else "unified"
        # _layout_mode can only be "split" or "unified" here (not "auto")
        return self._layout_mode

    @property
    def expanded(self) -> bool:
        """Whether all equal-run lines are shown (collapse suppressed)."""
        return self._expanded

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def set_palette(self, palette: Palette) -> None:
        """Replace the palette; clears the highlight cache and repaints."""
        self._palette = palette
        self._highlight_cache.clear()
        self._visible_rows = None
        self.refresh()

    def expand_all(self) -> None:
        """Show all collapsed equal runs."""
        self._expanded = True
        self._visible_rows = None
        self.refresh()

    def collapse_all(self) -> None:
        """Re-collapse equal runs to context_lines head + tail."""
        self._expanded = False
        self._visible_rows = None
        self.refresh()

    # ------------------------------------------------------------------
    # Textual action handlers (key bindings)
    # ------------------------------------------------------------------

    def action_toggle_layout(self) -> None:
        """Cycle the forced layout: None → split → unified → None."""
        if self._forced_layout is None:
            # Determine current auto choice and flip it
            current = self.effective_layout
            self._forced_layout = "unified" if current == "split" else "split"
        elif self._forced_layout == "split":
            self._forced_layout = "unified"
        else:
            self._forced_layout = None
        self._visible_rows = None
        self.refresh()

    def action_toggle_expand(self) -> None:
        """Toggle expand/collapse."""
        if self._expanded:
            self.collapse_all()
        else:
            self.expand_all()

    def action_scroll_left(self) -> None:
        """Scroll code view left."""
        self._h_offset = max(0, self._h_offset - self._H_SCROLL_STEP)
        self._visible_rows = None
        self.refresh()

    def action_scroll_right(self) -> None:
        """Scroll code view right."""
        self._h_offset += self._H_SCROLL_STEP
        self._visible_rows = None
        self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        """Build and return a Rich ``Text`` renderable for Textual."""
        rows = self._build_render_rows()
        # Use fg_base as the palette-driven default text color so any unthemed
        # spans (padding, gutter non-syntax text) inherit the base foreground.
        result = Text(style=self._palette.fg_base)
        for i, row in enumerate(rows):
            if i:
                result.append("\n")
            result.append_text(row)
        return result

    # ------------------------------------------------------------------
    # Diff computation (pure, cached)
    # ------------------------------------------------------------------

    def _compute_diff(self) -> list[DiffLine]:
        """Build a ``DiffLine`` list from the old/new texts."""
        old_lines = self._old_text.splitlines()
        new_lines = self._new_text.splitlines()
        result: list[DiffLine] = []
        sm = difflib.SequenceMatcher(None, old_lines, new_lines)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for offset, (ol, nl) in enumerate(
                    zip(old_lines[i1:i2], new_lines[j1:j2])
                ):
                    result.append(
                        DiffLine(
                            kind="eq",
                            old_no=i1 + offset + 1,
                            new_no=j1 + offset + 1,
                            text=ol,
                        )
                    )
            elif tag == "replace":
                # Emit deletes then adds (standard unified diff order)
                for offset, line in enumerate(old_lines[i1:i2]):
                    result.append(
                        DiffLine(
                            kind="del",
                            old_no=i1 + offset + 1,
                            new_no=None,
                            text=line,
                        )
                    )
                for offset, line in enumerate(new_lines[j1:j2]):
                    result.append(
                        DiffLine(
                            kind="add",
                            old_no=None,
                            new_no=j1 + offset + 1,
                            text=line,
                        )
                    )
            elif tag == "delete":
                for offset, line in enumerate(old_lines[i1:i2]):
                    result.append(
                        DiffLine(
                            kind="del",
                            old_no=i1 + offset + 1,
                            new_no=None,
                            text=line,
                        )
                    )
            elif tag == "insert":
                for offset, line in enumerate(new_lines[j1:j2]):
                    result.append(
                        DiffLine(
                            kind="add",
                            old_no=None,
                            new_no=j1 + offset + 1,
                            text=line,
                        )
                    )
        return result

    # ------------------------------------------------------------------
    # Truncation / visible rows
    # ------------------------------------------------------------------

    def _compute_visible_rows(self) -> list[tuple[str, str, DiffLine | None]]:
        """Reduce diff_lines to visible rows applying equal-run truncation.

        Returns a list of ``(kind, text, diffline)`` tuples where ``kind`` is
        one of ``"add"`` / ``"del"`` / ``"eq"`` / ``"hidden"``.  ``diffline``
        is ``None`` for ``"hidden"`` placeholder rows.
        """
        lines = self.diff_lines
        if self._expanded or not lines:
            return [(dl.kind, dl.text, dl) for dl in lines]

        ctx = self._context_lines
        rows: list[tuple[str, str, DiffLine | None]] = []
        n = len(lines)
        i = 0

        while i < n:
            dl = lines[i]
            if dl.kind != "eq":
                rows.append((dl.kind, dl.text, dl))
                i += 1
                continue

            # Scan the equal run
            run_start = i
            while i < n and lines[i].kind == "eq":
                i += 1
            run_end = i  # exclusive
            run_len = run_end - run_start

            # Determine context windows
            need_head = run_start > 0  # there was something before
            need_tail = run_end < n    # there's something after

            if run_len <= ctx * 2 or not (need_head or need_tail):
                # Short run or isolated run → show all
                for j in range(run_start, run_end):
                    rows.append(("eq", lines[j].text, lines[j]))
            else:
                # Long run → head ctx + hidden marker + tail ctx
                head_end = run_start + (ctx if need_head else 0)
                tail_start = run_end - (ctx if need_tail else 0)

                if need_head:
                    for j in range(run_start, head_end):
                        rows.append(("eq", lines[j].text, lines[j]))

                hidden = tail_start - head_end
                if hidden > 0:
                    rows.append(("hidden", f"… {hidden} lines hidden — expand", None))

                if need_tail:
                    for j in range(tail_start, run_end):
                        rows.append(("eq", lines[j].text, lines[j]))

        return rows

    # ------------------------------------------------------------------
    # Syntax highlighting (cached)
    # ------------------------------------------------------------------

    def _cache_key(self, side: Literal["old", "new"]) -> str:
        """Compute the cache key for one side's highlighted Text list."""
        content = self._old_text if side == "old" else self._new_text
        # Identity hash: (content bytes, theme, palette identity via id)
        payload = f"{content}\x00{self._syntax_theme}\x00{id(self._palette)}"
        return hashlib.md5(payload.encode(), usedforsecurity=False).hexdigest()  # noqa: S324

    def _highlighted_lines(self, side: Literal["old", "new"]) -> Lines:
        """Return a per-line ``Text`` sequence for one side.  Cached by content+theme+palette."""
        key = self._cache_key(side)
        if key in self._highlight_cache:
            return self._highlight_cache[key]

        # Cache miss → compute
        self._highlight_misses += 1
        content = self._old_text if side == "old" else self._new_text
        fp = self._file_path or ""
        lexer = Syntax.guess_lexer(fp, code=content) if content else "default"
        syntax = Syntax(content, lexer, theme=self._syntax_theme)
        highlighted: Text = syntax.highlight(content)
        lines = highlighted.split("\n")
        self._highlight_cache[key] = lines
        return lines

    # ------------------------------------------------------------------
    # Render row building
    # ------------------------------------------------------------------

    def _build_render_rows(self) -> list[Text]:
        """Build the list of Rich ``Text`` rows to display."""
        layout = self.effective_layout
        width = max(self.size.width, 1)
        p = self._palette

        old_hl = self._highlighted_lines("old")
        new_hl = self._highlighted_lines("new")

        # Compute max line-number width for padding
        all_old_nos = [dl.old_no for dl in self.diff_lines if dl.old_no is not None]
        all_new_nos = [dl.new_no for dl in self.diff_lines if dl.new_no is not None]
        max_old = max(all_old_nos) if all_old_nos else 1
        max_new = max(all_new_nos) if all_new_nos else 1
        ln_w = len(str(max(max_old, max_new)))  # digit width

        if layout == "split":
            return self._build_split_rows(old_hl, new_hl, width, ln_w, p)
        return self._build_unified_rows(old_hl, new_hl, width, ln_w, p)

    def _gutter_text(self, sign: str, no: int | None, ln_w: int, color: str) -> Text:
        """Build a styled gutter cell: sign + line number."""
        if not self._line_numbers:
            t = Text(sign, style=f"{color}")
            return t
        no_str = str(no).rjust(ln_w) if no is not None else " " * ln_w
        t = Text(f"{sign}{no_str} ", style=color)
        return t

    def _code_cell(
        self,
        hl_lines: Lines,
        line_index: int,
        col_width: int,
        h_offset: int,
        bg_color: str | None,
    ) -> Text:
        """Build a code cell of exactly col_width chars from highlighted lines.

        Applies horizontal scroll offset, truncates with ``…``, overlays bg tint.
        """
        if 0 <= line_index < len(hl_lines):
            line_text = hl_lines[line_index].copy()
        else:
            line_text = Text("")

        raw = line_text.plain
        # Apply horizontal offset
        if h_offset > 0 and len(raw) > h_offset:
            # Slice the Text at h_offset
            line_text = line_text[h_offset:]
            raw = line_text.plain
            # Prepend a leading ellipsis marker
            prefix = Text("…", style=f"{self._palette.muted}")
            line_text = Text.assemble(prefix, line_text)
            raw = line_text.plain
        elif h_offset > 0:
            line_text = Text("")
            raw = ""

        # Truncate to col_width
        if len(raw) > col_width:
            line_text = line_text[:col_width - 1]
            line_text.append("…", style=f"{self._palette.muted}")
            raw = line_text.plain

        # Pad to exactly col_width
        pad_len = col_width - len(line_text.plain)
        if pad_len > 0:
            line_text.append(" " * pad_len)

        # Overlay background tint
        if bg_color:
            line_text.stylize(f"on {bg_color}")

        return line_text

    def _build_split_rows(
        self,
        old_hl: Lines,
        new_hl: Lines,
        width: int,
        ln_w: int,
        p: Palette,
    ) -> list[Text]:
        """Build rows for side-by-side split view."""
        sep = Text("│", style=p.border)
        gutter_w = (1 + ln_w + 1) if self._line_numbers else 2  # sign + digits + space
        half = max((width - gutter_w * 2 - 1) // 2, 4)

        rows: list[Text] = []

        for kind, text, dl in self.visible_rows:
            if kind == "hidden":
                row = Text(
                    text.center(width),
                    style=p.muted,
                )
                rows.append(row)
                continue

            if kind == "eq":
                bg = None
                sign = " "
                ln_color = p.diff_eq
            elif kind == "add":
                bg = p.diff_add
                sign = "+"
                ln_color = p.success
            else:  # del
                bg = p.diff_del
                sign = "-"
                ln_color = p.error

            # Left (old) side
            if kind in ("eq", "del") and dl is not None:
                left_gutter = self._gutter_text(sign, dl.old_no, ln_w, ln_color)
                hl_idx = (dl.old_no - 1) if dl.old_no is not None else -1
                left_code = self._code_cell(old_hl, hl_idx, half, self._h_offset, bg)
            else:
                # add line → left side is blank
                left_gutter = self._gutter_text(" ", None, ln_w, ln_color)
                left_code = self._code_cell(Lines(), -1, half, 0, bg)

            # Right (new) side
            if kind in ("eq", "add") and dl is not None:
                right_gutter = self._gutter_text(sign, dl.new_no, ln_w, ln_color)
                hl_idx = (dl.new_no - 1) if dl.new_no is not None else -1
                right_code = self._code_cell(new_hl, hl_idx, half, self._h_offset, bg)
            else:
                # del line → right side is blank
                right_gutter = self._gutter_text(" ", None, ln_w, ln_color)
                right_code = self._code_cell(Lines(), -1, half, 0, bg)

            row = Text.assemble(left_gutter, left_code, sep, right_gutter, right_code)
            rows.append(row)

        return rows

    def _build_unified_rows(
        self,
        old_hl: Lines,
        new_hl: Lines,
        width: int,
        ln_w: int,
        p: Palette,
    ) -> list[Text]:
        """Build rows for unified (single-column) view."""
        gutter_w = (2 + ln_w * 2 + 1) if self._line_numbers else 2
        code_w = max(width - gutter_w, 4)

        rows: list[Text] = []
        for kind, text, dl in self.visible_rows:
            if kind == "hidden":
                rows.append(Text(text.center(width), style=p.muted))
                continue

            if kind == "eq":
                bg = None
                sign = " "
                ln_color = p.diff_eq
                hl_lines = new_hl  # use new side for eq
                hl_idx = (dl.new_no - 1) if (dl and dl.new_no) else -1
                ln_a = dl.old_no if dl else None
                ln_b = dl.new_no if dl else None
            elif kind == "add":
                bg = p.diff_add
                sign = "+"
                ln_color = p.success
                hl_lines = new_hl
                hl_idx = (dl.new_no - 1) if (dl and dl.new_no) else -1
                ln_a = None
                ln_b = dl.new_no if dl else None
            else:  # del
                bg = p.diff_del
                sign = "-"
                ln_color = p.error
                hl_lines = old_hl
                hl_idx = (dl.old_no - 1) if (dl and dl.old_no) else -1
                ln_a = dl.old_no if dl else None
                ln_b = None

            if self._line_numbers:
                old_s = str(ln_a).rjust(ln_w) if ln_a is not None else " " * ln_w
                new_s = str(ln_b).rjust(ln_w) if ln_b is not None else " " * ln_w
                gutter = Text(f"{sign}{old_s} {new_s} ", style=ln_color)
            else:
                gutter = Text(f"{sign} ", style=ln_color)

            code = self._code_cell(hl_lines, hl_idx, code_w, self._h_offset, bg)
            row = Text.assemble(gutter, code)
            rows.append(row)

        return rows

