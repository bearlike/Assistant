#!/usr/bin/env python3
"""StatusLine — the IDE-style status bar docked above the keybinding Footer.

One compact horizontal line of session context, in the spirit of a good editor's
bottom bar (and the ``statusLine`` script contract in
:mod:`mewbo_cli.tui.status.statusline`):

    󰀄 user@host · openai/gpt-oss-120b · 󰉋 ~/project · 󰘬 main 󰏗 2 · ↑12.3K ↓4.1K

The facets, left→right: ``user@host`` · model · working dir (``~`` shortened) ·
git branch/worktree (with a stash count when the stash is non-empty) · the
**session token totals**, input/output faceted (``↑`` input, ``↓`` output) —
deliberately the *tokens consumed this session*, never a subscription quota.

The widget is dumb and pure: it renders whatever :class:`StatusLineData` it is
handed via :meth:`apply`. The host/cwd are filled in by :meth:`StatusLineData.local`
so a bare App (no installer) still shows a useful line; the #156 installer drives
the live facets (branch/stash/tokens) on a refresh interval. Colors come from
theme CSS vars (``$accent`` / ``$success`` / ``$warning`` / ``$text-muted``) —
never hex. One atomic class.
"""

from __future__ import annotations

import getpass
import os
import socket
from dataclasses import dataclass, replace

from textual.reactive import reactive
from textual.widgets import Static

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.tui.status.context_meter import ContextMeter
from mewbo_cli.tui.widgets._markup import escape_markup as _escape


@dataclass(frozen=True)
class StatusLineData:
    """The facets the status line renders (all optional; missing ones are dropped).

    ``input_tokens`` / ``output_tokens`` are the session totals; ``stash`` is the
    git stash depth (rendered only when ``> 0``). ``worktree`` wins over
    ``branch`` when both are present (it is the more specific label).
    """

    host: str | None = None
    model: str | None = None
    cwd: str | None = None
    branch: str | None = None
    worktree: str | None = None
    stash: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def local(cls) -> StatusLineData:
        """Seed the static facets (``user@host`` and the working dir), never raising."""
        return cls(host=_local_host(), cwd=os.getcwd())

    def merged(self, other: StatusLineData) -> StatusLineData:
        """Overlay the non-default facets of ``other`` onto a copy of ``self``.

        Lets the installer refresh only the live facets (branch/stash/tokens/model)
        without clobbering the locally-seeded ``host``/``cwd``.
        """
        updates = {
            field: value
            for field, value in vars(other).items()
            if value not in (None, 0, "")
        }
        return replace(self, **updates)


class StatusLine(Static):
    """Single-line IDE-style status bar (model · cwd · branch · session tokens).

    Drive updates via :meth:`apply` (thread-safe through the reactive assignment
    the App turn worker / refresh tick triggers with ``call_from_thread``). The
    bar clips rather than wraps (``height: 1``) so it never steals rows from the
    composer above it.
    """

    DEFAULT_CSS = """
    StatusLine {
        height: 1;
        padding: 0 1;
        background: $panel;
        color: $text-muted;
        text-style: none;
    }
    """

    data: reactive[StatusLineData] = reactive(StatusLineData.local, always_update=True)

    def on_mount(self) -> None:
        """Render the seeded local facets once mounted."""
        self._refresh_content()

    # -- thread-safe update ----------------------------------------------

    def apply(self, data: StatusLineData) -> None:
        """Merge live facets over the current line (thread-safe via reactive)."""
        self.data = self.data.merged(data)

    def watch_data(self, _old: StatusLineData, _new: StatusLineData) -> None:
        """Re-render when any facet changes."""
        if self.is_mounted:
            self._refresh_content()

    # -- rendering --------------------------------------------------------

    def _refresh_content(self) -> None:
        self.update(self.markup())

    def markup(self) -> str:
        """Compose the facets into one Textual-markup line (pure; testable)."""
        d = self.data
        segments: list[str] = []

        if d.host:
            segments.append(f"[$text-muted]{ICONS.user} {_escape(_short(d.host, 28))}[/]")
        if d.model:
            segments.append(f"[$accent]{_escape(_short(d.model, 28))}[/]")
        if d.cwd:
            segments.append(f"[$text-muted]{ICONS.folder} {_escape(_short(_tilde(d.cwd), 32))}[/]")

        branch = d.worktree or d.branch
        if branch:
            seg = f"[$success]{ICONS.branch} {_escape(_short(branch, 22))}[/]"
            if d.stash > 0:
                seg += f" [$warning]{ICONS.stash} {d.stash}[/]"
            segments.append(seg)

        total = max(0, d.input_tokens) + max(0, d.output_tokens)
        if total > 0:
            up = ContextMeter.format_tokens(d.input_tokens)
            down = ContextMeter.format_tokens(d.output_tokens)
            # Token throughput reads like network activity: ↑ sent (accent) and
            # ↓ received (success), each with a space after the arrow and a wider
            # gap between the two so the glyphs never crowd the numbers.
            segments.append(f"[$accent]↑ {up}[/]   [$success]↓ {down}[/]")

        return "  [$text-muted]·[/]  ".join(segments)


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def _local_host() -> str | None:
    """``user@shorthost`` for the current process, or ``None`` if undiscoverable."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USER") or os.environ.get("USERNAME") or ""
    try:
        host = socket.gethostname().split(".", 1)[0]
    except Exception:
        host = ""
    label = f"{user}@{host}".strip("@")
    return label or None


def _tilde(path: str) -> str:
    """Replace a leading ``$HOME`` with ``~`` (cosmetic; never raises)."""
    try:
        home = os.path.expanduser("~")
    except Exception:
        return path
    if home and path.startswith(home):
        return "~" + path[len(home) :]
    return path


def _short(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars with a leading ellipsis for paths."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return "…" + text[-(limit - 1) :]


__all__ = ["StatusLine", "StatusLineData"]
