#!/usr/bin/env python3
"""SidebarView — ordered slot container wired to the sidebar-slot seam.

Placeholder for issue #150 (epic #149). Child #156 fills the sidebar by
registering slot factories on the same
:class:`~mewbo_cli.tui.seams.SidebarSlotRegistry` without touching this widget.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from mewbo_cli.tui.seams import SidebarSlotRegistry


class SidebarSection(VerticalScroll):
    """A titled sidebar facet: a bold header rule over one body widget (#161).

    Phase 4 splits the cramped sidebar into clearly delineated sections (Fleet ·
    Plan · Context). Each registered slot wraps its widget in one of these so the
    section reads as its own block — an uppercased ``$accent`` header with a thin
    rule, then the body — instead of a wall of undifferentiated lines.
    """

    DEFAULT_CSS = """
    SidebarSection {
        height: auto;
        max-height: 1fr;
        margin: 0 0 1 0;
        padding: 0;
    }
    SidebarSection > .section-header {
        text-style: bold;
        color: $accent;
        border-bottom: solid $panel;
        margin: 0 0 1 0;
    }
    """

    def __init__(self, title: str, body: Widget, *, id: str | None = None) -> None:  # noqa: A002
        """Wrap ``body`` under a bold ``title`` header rule."""
        super().__init__(id=id)
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        """Yield the header rule then the section body widget."""
        yield Static(self._title.upper(), classes="section-header")
        yield self._body


class SidebarView(VerticalScroll):
    """Scrollable sidebar whose children come from a :class:`SidebarSlotRegistry`.

    :meth:`compose` renders the current registered slots sorted by
    ``(order, name)``. Call :meth:`refresh_slots` after late registration to
    rebuild the child list without remounting the widget itself.
    """

    def __init__(
        self,
        registry: SidebarSlotRegistry,
        *,
        id: str | None = None,  # noqa: A002
    ) -> None:
        """Initialise the scroll container and store the slot registry.

        Args:
            registry: Provides the ordered ``(name, factory)`` pairs that
                compose the sidebar's children.
            id: Optional Textual DOM id forwarded to
                :class:`~textual.containers.VerticalScroll`.
        """
        super().__init__(id=id)
        self._registry = registry

    def compose(self) -> ComposeResult:
        """Yield one widget per registered slot, or a dim placeholder if empty.

        Slots are yielded in ``(order, name)`` order so layout is deterministic
        regardless of registration call order.
        """
        slots = self._registry.slots()
        if slots:
            for _name, factory in slots:
                yield factory()
        else:
            yield Static("—", classes="sidebar-empty")

    async def refresh_slots(self) -> None:
        """Rebuild children from the current registry state.

        Call this after registering new slots at runtime so the sidebar
        reflects the updated set without requiring a full remount.
        """
        await self.remove_children()
        slots = self._registry.slots()
        if slots:
            await self.mount(*[factory() for _name, factory in slots])
        else:
            await self.mount(Static("—", classes="sidebar-empty"))
