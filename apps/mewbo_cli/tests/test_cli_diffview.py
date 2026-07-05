"""Tests for cli_diffview.DiffView — TDD for task #153.

Test cases (required):
  1. split-vs-unified threshold: width 200 → split, width 80 → unified.
  2. truncation: long equal run collapses to "… N lines hidden" marker; expand reveals all.
  3. cache hit: render then resize → no new _highlight_misses.
  4. (good-to-have) 't' toggles layout; line-number digit padding; add/del tint.
"""

from __future__ import annotations

import asyncio
import re

from mewbo_cli.cli_diffview import DiffLine, DiffView
from mewbo_cli.cli_theme import DEFAULT_PALETTE, HYPER_PALETTE
from textual.app import App, ComposeResult

# ---------------------------------------------------------------------------
# Tiny host app for mounting DiffView
# ---------------------------------------------------------------------------


class DiffApp(App[None]):
    """Minimal host for testing DiffView in isolation."""

    def __init__(
        self,
        old: str,
        new: str,
        *,
        layout: str = "auto",
        split_min_width: int = 120,
        context_lines: int = 3,
        file_path: str | None = None,
    ) -> None:
        super().__init__()
        self._old = old
        self._new = new
        self._layout = layout
        self._split_min_width = split_min_width
        self._context_lines = context_lines
        self._file_path = file_path

    def compose(self) -> ComposeResult:
        yield DiffView(
            self._old,
            self._new,
            palette=DEFAULT_PALETTE,
            layout=self._layout,
            split_min_width=self._split_min_width,
            context_lines=self._context_lines,
            file_path=self._file_path,
        )


# ---------------------------------------------------------------------------
# 1. Layout threshold: auto mode picks split/unified by width
# ---------------------------------------------------------------------------


def test_split_layout_at_wide_width() -> None:
    """Width 200 > split_min_width(120) → auto selects 'split'."""

    async def _run() -> None:
        async with DiffApp("a\nb\n", "a\nc\n").run_test(size=(200, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            # Allow one refresh cycle so the widget is laid out
            await pilot.pause()
            assert dv.effective_layout == "split"

    asyncio.run(_run())


def test_unified_layout_at_narrow_width() -> None:
    """Width 80 < split_min_width(120) → auto selects 'unified'."""

    async def _run() -> None:
        async with DiffApp("a\nb\n", "a\nc\n").run_test(size=(80, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            assert dv.effective_layout == "unified"

    asyncio.run(_run())


def test_forced_split_layout_ignores_width() -> None:
    """layout='split' forces split even at narrow width."""

    async def _run() -> None:
        async with DiffApp(
            "a\nb\n", "a\nc\n", layout="split"
        ).run_test(size=(40, 20)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            assert dv.effective_layout == "split"

    asyncio.run(_run())


def test_forced_unified_layout_ignores_width() -> None:
    """layout='unified' forces unified even at wide width."""

    async def _run() -> None:
        async with DiffApp(
            "a\nb\n", "a\nc\n", layout="unified"
        ).run_test(size=(300, 20)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            assert dv.effective_layout == "unified"

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 2. Truncation: long equal run collapses; expand reveals all
# ---------------------------------------------------------------------------


def _make_long_equal(n_eq: int = 30) -> tuple[str, str]:
    """Return (old, new) where there's a change, then n_eq equal lines, then a change."""
    common = [f"line{i}" for i in range(n_eq)]
    old_lines = ["old_start"] + common + ["old_end"]
    new_lines = ["new_start"] + common + ["new_end"]
    return "\n".join(old_lines) + "\n", "\n".join(new_lines) + "\n"


def test_truncation_marker_present() -> None:
    """Long equal runs produce an '… N lines hidden' marker in the visible rows."""
    old, new = _make_long_equal(30)

    async def _run() -> None:
        async with DiffApp(old, new, context_lines=3).run_test(size=(160, 50)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # visible_rows is a list of (kind, text, diffline) tuples
            rows = dv.visible_rows
            hidden_markers = [r for r in rows if r[0] == "hidden"]
            assert hidden_markers, "expected at least one 'hidden' marker row"
            # The marker text must be honest about N (30 - 2*3 = 24 hidden)
            marker_text = hidden_markers[0][1]
            assert "hidden" in marker_text.lower()
            # Check a number is mentioned
            nums = re.findall(r"\d+", marker_text)
            assert nums, f"No number in marker text: {marker_text!r}"
            hidden_n = int(nums[0])
            assert hidden_n > 0

    asyncio.run(_run())


def test_expand_reveals_all_lines() -> None:
    """After expand(), hidden marker disappears and all lines are visible."""
    old, new = _make_long_equal(30)

    async def _run() -> None:
        async with DiffApp(old, new, context_lines=3).run_test(size=(160, 50)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # Should have hidden markers initially
            rows_before = dv.visible_rows
            hidden_before = [r for r in rows_before if r[0] == "hidden"]
            assert hidden_before, "expected hidden markers before expand"

            # Expand all
            dv.expand_all()
            await pilot.pause()

            rows_after = dv.visible_rows
            hidden_after = [r for r in rows_after if r[0] == "hidden"]
            assert not hidden_after, "no hidden markers expected after expand"

    asyncio.run(_run())


def test_expand_key_reveals_hidden() -> None:
    """Pressing 'e' toggles expanded state."""
    old, new = _make_long_equal(30)

    async def _run() -> None:
        async with DiffApp(old, new, context_lines=3).run_test(size=(160, 50)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # Focus the widget first
            dv.focus()
            await pilot.pause()

            assert not dv.expanded
            await pilot.press("e")
            await pilot.pause()
            assert dv.expanded

    asyncio.run(_run())


def test_hidden_marker_count_is_accurate() -> None:
    """The N in '… N lines hidden' must equal total_equal - 2*context_lines."""
    ctx = 3
    n_eq = 20
    expected_hidden = n_eq - 2 * ctx  # 14
    old, new = _make_long_equal(n_eq)

    async def _run() -> None:
        async with DiffApp(old, new, context_lines=ctx).run_test(size=(160, 50)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            rows = dv.visible_rows
            hidden_markers = [r for r in rows if r[0] == "hidden"]
            assert hidden_markers
            nums = re.findall(r"\d+", hidden_markers[0][1])
            hidden_n = int(nums[0])
            assert hidden_n == expected_hidden, (
                f"expected {expected_hidden} hidden, got {hidden_n}"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 3. Cache hit: resize does not recompute highlights
# ---------------------------------------------------------------------------


def test_cache_hit_on_resize() -> None:
    """Resizing the terminal (no content/palette change) must NOT increment _highlight_misses."""

    async def _run() -> None:
        old = "x = 1\ny = 2\n"
        new = "x = 1\ny = 3\n"
        async with DiffApp(old, new).run_test(size=(200, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # Force a render to populate the cache
            dv.refresh()
            await pilot.pause()
            misses_after_first = dv._highlight_misses

            # Resize to a narrower width: the cache key excludes width, so
            # the existing highlighted lines should be reused without re-compute.
            await pilot.resize_terminal(100, 40)
            await pilot.pause()
            misses_after_resize = dv._highlight_misses

            assert misses_after_resize == misses_after_first, (
                f"Expected no new misses on resize: "
                f"before={misses_after_first}, after={misses_after_resize}"
            )

    asyncio.run(_run())


def test_palette_change_invalidates_cache() -> None:
    """Calling set_palette() with a new palette must increment _highlight_misses."""

    async def _run() -> None:
        old = "x = 1\n"
        new = "x = 2\n"
        async with DiffApp(old, new).run_test(size=(160, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            dv.refresh()
            await pilot.pause()
            misses_before = dv._highlight_misses

            # Change palette → cache invalidated → next render should miss
            dv.set_palette(HYPER_PALETTE)
            dv.refresh()
            await pilot.pause()
            misses_after = dv._highlight_misses

            assert misses_after > misses_before, (
                f"Expected new misses after palette change: "
                f"before={misses_before}, after={misses_after}"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Good-to-have: 't' toggles layout
# ---------------------------------------------------------------------------


def test_t_key_toggles_layout() -> None:
    """Pressing 't' cycles the forced layout override."""

    async def _run() -> None:
        async with DiffApp("a\n", "b\n").run_test(size=(200, 20)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            dv.focus()
            await pilot.pause()

            layout_before = dv.effective_layout
            await pilot.press("t")
            await pilot.pause()
            layout_after = dv.effective_layout
            assert layout_after != layout_before, (
                f"layout should change on 't': {layout_before!r} → {layout_after!r}"
            )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Good-to-have: line-number digit padding
# ---------------------------------------------------------------------------


def test_line_number_padding() -> None:
    """Line numbers must be zero-padded to the max digit width."""
    # 10 lines → max line no = 10 → 2 digits → single-digit lines should be padded
    lines = [f"line{i}" for i in range(10)]
    old = "\n".join(lines) + "\n"
    new = old  # identical → all eq

    async def _run() -> None:
        async with DiffApp(old, new).run_test(size=(160, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # Access the DiffLine list
            diff_lines = dv.diff_lines
            # All should be "eq" kind
            assert all(dl.kind == "eq" for dl in diff_lines)
            # Line numbers should go up to 10
            new_nos = [dl.new_no for dl in diff_lines if dl.new_no is not None]
            assert max(new_nos) == 10  # 10 lines, 1-based

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# 4. Good-to-have: add/del lines carry the palette tint
# ---------------------------------------------------------------------------


def test_diff_lines_kinds() -> None:
    """Added and deleted lines are correctly classified."""
    old = "a\nb\nc\n"
    new = "a\nd\nc\n"

    async def _run() -> None:
        async with DiffApp(old, new).run_test(size=(160, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            diff_lines = dv.diff_lines
            kinds = [dl.kind for dl in diff_lines]
            assert "del" in kinds, "expected 'del' lines"
            assert "add" in kinds, "expected 'add' lines"
            assert "eq" in kinds, "expected 'eq' lines"

    asyncio.run(_run())


def test_diff_line_old_no_none_for_add() -> None:
    """Added lines must have old_no=None."""
    old = "a\n"
    new = "a\nb\n"

    async def _run() -> None:
        async with DiffApp(old, new).run_test(size=(160, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            add_lines = [dl for dl in dv.diff_lines if dl.kind == "add"]
            assert add_lines
            for dl in add_lines:
                assert dl.old_no is None

    asyncio.run(_run())


def test_diff_line_new_no_none_for_del() -> None:
    """Deleted lines must have new_no=None."""
    old = "a\nb\n"
    new = "a\n"

    async def _run() -> None:
        async with DiffApp(old, new).run_test(size=(160, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            del_lines = [dl for dl in dv.diff_lines if dl.kind == "del"]
            assert del_lines
            for dl in del_lines:
                assert dl.new_no is None

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Gutter line-number correctness for duplicate (identical) lines
# ---------------------------------------------------------------------------


def test_gutter_numbers_correct_for_repeated_lines() -> None:
    """Repeated identical lines (e.g. blank lines) must get strictly increasing gutter numbers.

    The old ``_find_diffline(kind, text)`` lookup always returned the FIRST
    matching DiffLine, so every repeated blank / ``pass`` / ``}`` rendered with
    the same number.  The fix threads each row's own DiffLine through so each
    row uses ITS OWN line numbers.
    """
    # Build a diff where the same text appears on multiple eq lines.
    # Three identical blank lines followed by a change, then three more blanks.
    old = "\n\n\nx = 1\n\n\n"
    new = "\n\n\nx = 2\n\n\n"

    async def _run() -> None:
        async with DiffApp(old, new, context_lines=10).run_test(size=(160, 40)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            # Extract old_no from each DiffLine that has one (eq + del)
            old_nos = [
                dl.old_no
                for dl in dv.diff_lines
                if dl.old_no is not None
            ]
            # Must be strictly increasing (no repeats)
            assert old_nos == sorted(set(old_nos)), (
                f"old_no values must be unique and increasing, got: {old_nos}"
            )
            # Same for new_no
            new_nos = [
                dl.new_no
                for dl in dv.diff_lines
                if dl.new_no is not None
            ]
            assert new_nos == sorted(set(new_nos)), (
                f"new_no values must be unique and increasing, got: {new_nos}"
            )
            # The visible_rows must carry the DiffLine references (not None for eq/add/del)
            for kind, _text, dl_ref in dv.visible_rows:
                if kind != "hidden":
                    assert dl_ref is not None, (
                        f"visible_rows row kind={kind!r} must carry its DiffLine"
                    )

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# DiffLine dataclass tests (pure, no app)
# ---------------------------------------------------------------------------


def test_diffline_eq_kind() -> None:
    dl = DiffLine(kind="eq", old_no=1, new_no=1, text="hello")
    assert dl.kind == "eq"
    assert dl.old_no == 1
    assert dl.new_no == 1
    assert dl.text == "hello"


def test_diffline_add_kind() -> None:
    dl = DiffLine(kind="add", old_no=None, new_no=2, text="new line")
    assert dl.kind == "add"
    assert dl.old_no is None
    assert dl.new_no == 2


def test_diffline_del_kind() -> None:
    dl = DiffLine(kind="del", old_no=3, new_no=None, text="old line")
    assert dl.kind == "del"
    assert dl.new_no is None
    assert dl.old_no == 3


def test_diffline_is_frozen() -> None:
    dl = DiffLine(kind="eq", old_no=1, new_no=1, text="x")
    params = getattr(dl, "__dataclass_params__", None)
    assert params is not None and params.frozen


# ---------------------------------------------------------------------------
# set_palette public method
# ---------------------------------------------------------------------------


def test_set_palette_updates_palette() -> None:
    """set_palette() must update the palette attribute."""

    async def _run() -> None:
        async with DiffApp("a\n", "b\n").run_test(size=(160, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            assert dv.palette is DEFAULT_PALETTE
            dv.set_palette(HYPER_PALETTE)
            assert dv.palette is HYPER_PALETTE

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Empty diffs
# ---------------------------------------------------------------------------


def test_empty_diff_no_crash() -> None:
    """Identical content → all eq lines, no crash."""

    async def _run() -> None:
        text = "line1\nline2\n"
        async with DiffApp(text, text).run_test(size=(120, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            assert all(dl.kind == "eq" for dl in dv.diff_lines)

    asyncio.run(_run())


def test_completely_different_no_crash() -> None:
    """Completely different content → only add/del lines, no crash."""

    async def _run() -> None:
        async with DiffApp("aaa\n", "bbb\n").run_test(size=(120, 30)) as pilot:
            dv = pilot.app.query_one(DiffView)
            await pilot.pause()
            kinds = {dl.kind for dl in dv.diff_lines}
            assert "eq" not in kinds, (
                f"completely different content must produce no 'eq' lines; got kinds={kinds}"
            )

    asyncio.run(_run())
