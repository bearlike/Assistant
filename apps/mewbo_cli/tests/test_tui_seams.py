#!/usr/bin/env python3
"""Tests for the foundation TUI extension seams.

These four seams are the stable extension points every Wave-2 child mounts
into without editing shared wiring:

- ``MessageRendererRegistry`` (consumed by transcript)
- ``SidebarSlotRegistry`` (consumed by agent panel/status)
- ``PermissionGateway`` (consumed by permission modal)
- ``InputGateway`` (consumed by input/completion)
"""

from __future__ import annotations

from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
    TranscriptItem,
)
from rich.text import Text

# --- TranscriptItem ------------------------------------------------------


def test_transcript_item_defaults_to_empty_payload() -> None:
    item = TranscriptItem(kind="user")
    assert item.kind == "user"
    assert item.payload == {}


def test_transcript_item_is_frozen() -> None:
    item = TranscriptItem(kind="assistant", payload={"text": "hi"})
    try:
        item.kind = "user"  # type: ignore[misc]
    except Exception as exc:  # FrozenInstanceError
        assert "cannot assign" in str(exc).lower() or "frozen" in str(exc).lower()
    else:  # pragma: no cover - dataclass must be frozen
        raise AssertionError("TranscriptItem should be frozen")


# --- MessageRendererRegistry ---------------------------------------------


def test_registry_routes_to_registered_renderer_by_kind() -> None:
    registry = MessageRendererRegistry()
    registry.register("assistant", lambda item: Text(f"A:{item.payload['text']}"))

    out = registry.render(TranscriptItem(kind="assistant", payload={"text": "hello"}))

    assert isinstance(out, Text)
    assert out.plain == "A:hello"


def test_registry_unknown_kind_uses_generic_fallback() -> None:
    registry = MessageRendererRegistry()
    out = registry.render(TranscriptItem(kind="totally-unknown", payload={"text": "x"}))
    # The shipped generic fallback renders the payload text, never raises.
    assert "x" in str(getattr(out, "plain", out))


def test_registry_fallback_is_overridable() -> None:
    registry = MessageRendererRegistry()
    registry.set_fallback(lambda item: Text("FALLBACK"))
    out = registry.render(TranscriptItem(kind="nope"))
    assert isinstance(out, Text)
    assert out.plain == "FALLBACK"


def test_registry_has_reports_registration() -> None:
    registry = MessageRendererRegistry()
    assert registry.has("tool") is False
    registry.register("tool", lambda item: Text("t"))
    assert registry.has("tool") is True


def test_registry_register_replaces_same_kind() -> None:
    registry = MessageRendererRegistry()
    registry.register("k", lambda item: Text("first"))
    registry.register("k", lambda item: Text("second"))
    assert registry.render(TranscriptItem(kind="k")).plain == "second"  # type: ignore[union-attr]


# --- SidebarSlotRegistry -------------------------------------------------


def test_sidebar_slots_returned_sorted_by_order_then_name() -> None:
    registry = SidebarSlotRegistry()
    registry.register("zebra", lambda: "z", order=10)
    registry.register("alpha", lambda: "a", order=10)
    registry.register("first", lambda: "f", order=1)

    names = [name for name, _ in registry.slots()]
    assert names == ["first", "alpha", "zebra"]


def test_sidebar_slot_factory_is_preserved() -> None:
    registry = SidebarSlotRegistry()
    sentinel = object()
    registry.register("s", lambda: sentinel)  # type: ignore[arg-type,return-value]
    _, factory = registry.slots()[0]
    assert factory() is sentinel


def test_sidebar_unregister_removes_slot() -> None:
    registry = SidebarSlotRegistry()
    registry.register("s", lambda: "x")  # type: ignore[arg-type,return-value]
    registry.unregister("s")
    assert registry.slots() == []


def test_sidebar_register_replaces_same_name() -> None:
    registry = SidebarSlotRegistry()
    registry.register("s", lambda: "old", order=5)  # type: ignore[arg-type,return-value]
    registry.register("s", lambda: "new", order=5)  # type: ignore[arg-type,return-value]
    slots = registry.slots()
    assert len(slots) == 1
    assert slots[0][1]() == "new"


# --- PermissionGateway ---------------------------------------------------


class _Step:
    """Minimal ActionStep stand-in — the gateway only passes it through."""

    tool_id = "shell"
    operation = "run"
    tool_input = "ls"


def test_permission_auto_approve_short_circuits() -> None:
    gateway = PermissionGateway(auto_approve=lambda: True)
    assert gateway(_Step()) is True  # type: ignore[arg-type]


def test_permission_default_decision_is_deny() -> None:
    # esc=deny safe default before the App injects the modal decision.
    gateway = PermissionGateway(auto_approve=lambda: False)
    assert gateway(_Step()) is False  # type: ignore[arg-type]


def test_permission_uses_injected_decision_when_not_auto() -> None:
    seen: list[object] = []

    def decide(step: object) -> bool:
        seen.append(step)
        return True

    gateway = PermissionGateway(auto_approve=lambda: False)
    gateway.set_decision(decide)  # type: ignore[arg-type]
    step = _Step()
    assert gateway(step) is True  # type: ignore[arg-type]
    assert seen == [step]


def test_permission_auto_beats_decision() -> None:
    gateway = PermissionGateway(auto_approve=lambda: True)
    gateway.set_decision(lambda step: False)  # type: ignore[arg-type]
    assert gateway(_Step()) is True  # type: ignore[arg-type]


# --- InputGateway --------------------------------------------------------


def test_input_default_completion_is_empty() -> None:
    gateway = InputGateway()
    assert gateway.complete("/he") == []


def test_input_uses_injected_provider() -> None:
    gateway = InputGateway()
    gateway.set_completion_provider(lambda text: [f"{text}lp", f"{text}ist"])
    assert gateway.complete("/he") == ["/help", "/heist"]


def test_input_completion_never_raises() -> None:
    gateway = InputGateway()

    def boom(text: str) -> list[str]:
        raise RuntimeError("provider exploded")

    gateway.set_completion_provider(boom)
    # A misbehaving provider degrades to no suggestions, never breaks the prompt.
    assert gateway.complete("x") == []
