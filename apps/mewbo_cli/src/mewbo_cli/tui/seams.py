#!/usr/bin/env python3
"""Stable extension seams for the Mewbo TUI (issue #150, epic #149).

These four small, documented injection points are the keystone deliverable:
later children add a widget/behaviour by *registering at a seam* — never by
editing ``MewboApp`` or other shared wiring. Each seam is one atomic class:

- :class:`MessageRendererRegistry` — keyed message/tool renderers + a generic
  fallback. Consumed by the transcript child (#152).
- :class:`SidebarSlotRegistry` — ordered sidebar slot factories. Consumed by
  the agent-panel/status child (#156).
- :class:`PermissionGateway` — the tool-approval ``approval_callback`` threaded
  into ``SessionRuntime.run_sync``. Consumed by the permission child (#154).
- :class:`InputGateway` — the input completion source. Consumed by the
  input/completion child (#155).

The seams deliberately carry *no* product behaviour beyond a safe default —
they exist so the App stays closed to modification while open to extension.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.text import Text

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep
    from rich.console import RenderableType
    from textual.widget import Widget


# --- (a) message-renderer registry ---------------------------------------


@dataclass(frozen=True)
class TranscriptItem:
    """One unit of transcript content.

    ``kind`` keys the renderer (``"user"``, ``"assistant"``, ``"tool"``,
    ``"notice"``, …); ``payload`` carries the data that renderer needs. Frozen
    so an item handed to the transcript can never be mutated after the fact.
    """

    kind: str
    payload: Mapping[str, Any] = field(default_factory=dict)


MessageRenderer = Callable[[TranscriptItem], "RenderableType"]
"""A renderer turns a :class:`TranscriptItem` into any Rich renderable."""


def _default_message_renderer(item: TranscriptItem) -> RenderableType:
    """Generic fallback: render ``payload['text']`` (or a repr) as plain text.

    Never raises — an unknown kind degrades to readable text rather than
    breaking the transcript.
    """
    text = item.payload.get("text")
    if text is None:
        text = "" if not item.payload else repr(dict(item.payload))
    return Text(str(text))


class MessageRendererRegistry:
    """Route a :class:`TranscriptItem` to a renderer keyed by its ``kind``.

    The transcript child (#152) registers rich renderers (assistant markdown,
    tool cards, diffs) via :meth:`register` without touching ``MewboApp``::

        app.messages.register("tool", render_tool_card)

    Unknown kinds fall through to the generic fallback (overridable via
    :meth:`set_fallback`).
    """

    def __init__(self) -> None:
        """Start with no kind-specific renderers and the generic fallback."""
        self._renderers: dict[str, MessageRenderer] = {}
        self._fallback: MessageRenderer = _default_message_renderer

    def register(self, kind: str, renderer: MessageRenderer) -> None:
        """Bind ``kind`` to ``renderer`` (replacing any prior binding)."""
        self._renderers[kind] = renderer

    def set_fallback(self, renderer: MessageRenderer) -> None:
        """Replace the generic fallback used for unregistered kinds."""
        self._fallback = renderer

    def has(self, kind: str) -> bool:
        """Return whether a renderer is registered for ``kind``."""
        return kind in self._renderers

    def render(self, item: TranscriptItem) -> RenderableType:
        """Render ``item`` via its kind's renderer, else the fallback."""
        return self._renderers.get(item.kind, self._fallback)(item)


# --- (b) sidebar slot registry -------------------------------------------


SidebarSlot = Callable[[], "Widget"]
"""A slot factory returns a fresh Textual widget when the sidebar mounts."""


class SidebarSlotRegistry:
    """Ordered registry of sidebar slot factories.

    The agent-panel/status child (#156) registers fleet-tree, context-%, cost
    and branch slots without touching ``MewboApp``::

        app.sidebar_slots.register("fleet", build_fleet_tree, order=10)

    :meth:`slots` returns ``(name, factory)`` pairs sorted by ``(order, name)``
    so registration call order never affects layout.
    """

    def __init__(self) -> None:
        """Start with no registered slots."""
        self._slots: dict[str, tuple[int, SidebarSlot]] = {}

    def register(self, name: str, factory: SidebarSlot, *, order: int = 100) -> None:
        """Register ``name`` → ``factory`` at ``order`` (replacing same name)."""
        self._slots[name] = (order, factory)

    def unregister(self, name: str) -> None:
        """Remove ``name`` if present (no-op otherwise)."""
        self._slots.pop(name, None)

    def slots(self) -> list[tuple[str, SidebarSlot]]:
        """Return ``(name, factory)`` pairs sorted by ``(order, name)``."""
        ordered = sorted(self._slots.items(), key=lambda kv: (kv[1][0], kv[0]))
        return [(name, factory) for name, (_order, factory) in ordered]


# --- (c) permission gateway ----------------------------------------------


PermissionDecision = Callable[["ActionStep"], bool]
"""Decide one tool call: ``True`` approves, ``False`` denies."""


class PermissionGateway:
    """Tool-approval injection point passed to ``run_sync`` as ``approval_callback``.

    ``auto_approve`` (read live each call, so ``/automatic`` mid-session takes
    effect) short-circuits to approve. Otherwise the injected ``decision`` runs;
    its default is **deny** (esc=deny safe default) until the permission child
    (#154) swaps in the modal-backed decision::

        app.permission.set_decision(modal_decision_fn)
    """

    def __init__(
        self,
        *,
        auto_approve: Callable[[], bool],
        decision: PermissionDecision | None = None,
    ) -> None:
        """Bind the live ``auto_approve`` predicate and optional decision."""
        self._auto_approve = auto_approve
        self._decision: PermissionDecision = decision or (lambda step: False)

    def set_decision(self, decision: PermissionDecision) -> None:
        """Replace the decision used when auto-approve is off (#154)."""
        self._decision = decision

    def __call__(self, step: ActionStep) -> bool:
        """Approve ``step`` if auto-approve is on, else defer to the decision."""
        if self._auto_approve():
            return True
        return self._decision(step)


# --- (d) input/completion gateway ----------------------------------------


CompletionProvider = Callable[[str], list[str]]
"""Map the text before the cursor to a list of completion strings."""


class InputGateway:
    """Input-completion injection point consulted by the input widget.

    The input/completion child (#155) replaces the default (empty) provider
    with the full ``@file`` / ``/command`` / skill completer::

        app.input.set_completion_provider(mewbo_completion_fn)

    :meth:`complete` never raises — a misbehaving provider degrades to no
    suggestions rather than breaking the prompt.
    """

    def __init__(self, provider: CompletionProvider | None = None) -> None:
        """Bind ``provider`` (default: an empty completion source)."""
        self._provider: CompletionProvider = provider or (lambda text: [])

    def set_completion_provider(self, provider: CompletionProvider) -> None:
        """Replace the completion source (#155)."""
        self._provider = provider

    def complete(self, text: str) -> list[str]:
        """Return completions for ``text``; empty on any provider error."""
        try:
            return list(self._provider(text))
        except Exception:
            return []


__all__ = [
    "CompletionProvider",
    "InputGateway",
    "MessageRenderer",
    "MessageRendererRegistry",
    "PermissionDecision",
    "PermissionGateway",
    "SidebarSlot",
    "SidebarSlotRegistry",
    "TranscriptItem",
]
