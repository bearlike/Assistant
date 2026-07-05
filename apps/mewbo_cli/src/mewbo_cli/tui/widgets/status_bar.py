#!/usr/bin/env python3
"""StatusBar — the context-window + cost gauge sidebar slot (issue #156).

The second sidebar slot. One compact gauge row of the two facets that belong
*next to the agent fleet tree* — context-window pressure and session cost:

    󰓅 ctx ~12.3% · $0.0123

Deliberately NOT a vitals duplicate of the footer status line
(:class:`~mewbo_cli.tui.widgets.status_line.StatusLine`): model, working dir,
git branch and the raw token totals all live in the footer. This widget owns the
two things the footer does not — how full the context window is and what the
session has cost — so no information is shown in both places.

Rules from the brief:
- context-window **%** computed by :class:`~mewbo_cli.tui.status.context_meter.ContextMeter`
  (``used/total``); **>80% → warning style**; a ``~`` prefix when the total is a
  configured estimate rather than the model's real window.
- **cost** from tokens (LiteLLM pricing); ``—`` shown honestly when no price
  source exists for the model.

The controller drives it with :meth:`apply_state` from the App turn worker /
status refresh tick via ``call_from_thread``. ``model`` and the token counts are
carried on the state only to feed the meter — they are not rendered. Colors come
from theme CSS vars (``$warning`` / ``$success`` / ``$text-muted``) — never hex.
One atomic class.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.reactive import reactive
from textual.widgets import Static

from mewbo_cli.cli_icons import ICONS
from mewbo_cli.tui.status.context_meter import ContextMeter


@dataclass(frozen=True)
class StatusState:
    """The inputs to the context/cost gauge.

    ``model`` + ``used_tokens`` resolve the context-window %, ``model`` +
    ``input_tokens``/``output_tokens`` resolve the cost — none of these are
    rendered as text; only the computed gauge + cost are. ``provider`` /
    ``branch`` / ``worktree`` are retained for wire compatibility but unused now
    that identity/branch live in the footer status line.
    """

    model: str | None = None
    provider: str | None = None
    used_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    branch: str | None = None
    worktree: str | None = None


class StatusBar(Static):
    """Sidebar widget rendering model/provider/ctx-%/cost/tokens/branch.

    Inject a :class:`ContextMeter` (DI for testability); defaults to one reading
    live config. Drive updates via :meth:`apply_state` (thread-safe through the
    reactive assignment the App turn worker triggers with ``call_from_thread``).
    """

    DEFAULT_CSS = """
    StatusBar {
        height: auto;
        padding: 0 0 1 0;
    }
    """

    state: reactive[StatusState] = reactive(StatusState, always_update=True)

    def __init__(
        self,
        *,
        meter: ContextMeter | None = None,
        id: str | None = None,  # noqa: A002 - Textual DOM id
    ) -> None:
        """Bind the :class:`ContextMeter` (default: live-config) and forward ``id``."""
        super().__init__("", id=id)
        self._meter = meter or ContextMeter()

    def on_mount(self) -> None:
        """Render the initial state once mounted."""
        self._refresh_content()

    # -- thread-safe update ----------------------------------------------

    def apply_state(self, state: StatusState) -> None:
        """Replace the rendered vitals (thread-safe via reactive assignment)."""
        self.state = state

    def watch_state(self, _old: StatusState, _new: StatusState) -> None:
        """Re-render when the state changes."""
        if self.is_mounted:
            self._refresh_content()

    # -- rendering --------------------------------------------------------

    def _refresh_content(self) -> None:
        """Compose the vitals into the Static content (Textual markup string).

        ``Static`` parses ``[$var]…[/]`` markup itself (including theme color
        variables), so we hand it the joined markup string directly rather than
        a pre-built ``Content`` object.
        """
        self.update("\n".join(self.lines()))

    def lines(self) -> list[str]:
        """Return the markup lines for the current state (pure; testable).

        ONE gauge row: context-window % (warning >80, ``~`` when the window is a
        configured estimate) and the session cost. Identity/branch/raw-tokens are
        intentionally absent — they live in the footer status line.
        """
        st = self.state
        usage = self._meter.usage(used_tokens=st.used_tokens, model=st.model)
        prefix = "~" if usage.estimated else ""
        ctx_color = "$warning" if usage.over_warning else "$success"
        ctx = f"[$text-muted]{ICONS.gauge} ctx[/] [{ctx_color}]{prefix}{usage.percent:.1f}%[/]"
        cost = self._meter.cost(
            input_tokens=st.input_tokens,
            output_tokens=st.output_tokens,
            model=st.model,
        )
        return [f"{ctx} [$text-muted]· {ContextMeter.format_cost(cost)}[/]"]


__all__ = ["StatusBar", "StatusState"]
