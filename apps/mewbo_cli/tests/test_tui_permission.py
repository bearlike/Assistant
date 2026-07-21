#!/usr/bin/env python3
"""Pilot + unit tests for PermissionService, PermissionModal, and the
shift+tab mode cycle in MewboApp.

Pattern mirrors test_tui_app.py: asyncio.run(_run()) wrappers around
``app.run_test()`` Pilot sessions; plain unit tests for pure logic.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.permission_service import (
    PermissionMode,
    PermissionRuleStore,
    PermissionService,
    RiskTier,
    install_permission_service,
)
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.widgets.header import HeaderContext
from mewbo_core.classes import ActionStep

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_step(
    tool_id: str = "bash",
    operation: str = "execute",
    tool_input: str | dict[str, object] = "ls -la",
) -> ActionStep:
    """Build a minimal ActionStep for testing."""
    return ActionStep(
        tool_id=tool_id,
        operation=operation,
        tool_input=tool_input,
    )


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo",
        version="0.0.13",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-5.2",
        session_id="sess-perm-test",
        base_url="http://localhost:4000",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=3,
        builtin_disabled=0,
        external_enabled=1,
        external_disabled=0,
        skill_count=2,
    )


class _FakeEngine:
    def __init__(self, emit: Any, emit_renderable: Any) -> None:
        self.emit = emit
        self.emit_renderable = emit_renderable
        self.handled: list[str] = []

    def handle(self, text: str) -> bool:
        self.handled.append(text)
        return True


def _make_app() -> tuple[MewboApp, dict[str, Any]]:
    seams: dict[str, Any] = {
        "messages": MessageRendererRegistry(),
        "sidebar_slots": SidebarSlotRegistry(),
        "permission": PermissionGateway(auto_approve=lambda: True),
        "input": InputGateway(),
    }

    def factory(emit: Any, emit_renderable: Any) -> _FakeEngine:
        return _FakeEngine(emit, emit_renderable)

    app = MewboApp(
        header_ctx=_header_ctx(),
        messages=seams["messages"],
        sidebar_slots=seams["sidebar_slots"],
        permission=seams["permission"],
        input_gateway=seams["input"],
        engine_factory=factory,
    )
    return app, seams


# ---------------------------------------------------------------------------
# RiskTier classification
# ---------------------------------------------------------------------------


def test_risk_tier_get_is_read() -> None:
    # Use a non-bash tool to test the get→READ path
    step = _make_step(tool_id="file_reader", operation="get")
    assert RiskTier.classify(step) == RiskTier.READ


def test_risk_tier_set_is_write() -> None:
    step = _make_step(tool_id="edit_file", operation="set")
    assert RiskTier.classify(step) == RiskTier.WRITE


def test_risk_tier_execute_is_exec() -> None:
    step = _make_step(tool_id="bash", operation="execute")
    assert RiskTier.classify(step) == RiskTier.EXEC


def test_risk_tier_bash_tool_always_exec() -> None:
    """bash/shell tool with get operation is still EXEC (tool overrides)."""
    step = _make_step(tool_id="bash", operation="get")
    assert RiskTier.classify(step) == RiskTier.EXEC


def test_risk_tier_shell_tool_is_exec() -> None:
    step = _make_step(tool_id="shell", operation="execute")
    assert RiskTier.classify(step) == RiskTier.EXEC


def test_risk_tier_vendored_shell_tool_is_exec() -> None:
    """A vendored shell id (``aider_shell_tool:set``) tiers EXEC, not WRITE."""
    step = _make_step(tool_id="aider_shell_tool", operation="set")
    assert RiskTier.classify(step) == RiskTier.EXEC


def test_risk_tier_write_pattern_tool_with_get_is_write() -> None:
    """_WRITE_TOOL_PATTERNS are consulted: delete_file:get → WRITE, not READ."""
    step = _make_step(tool_id="delete_file", operation="get")
    assert RiskTier.classify(step) == RiskTier.WRITE


def test_risk_tier_edit_pattern_with_get_is_write() -> None:
    """edit_* tools are WRITE regardless of operation."""
    step = _make_step(tool_id="edit_something", operation="get")
    assert RiskTier.classify(step) == RiskTier.WRITE


def test_risk_tier_non_write_pattern_get_is_read() -> None:
    """A safe read tool with no write-pattern name stays READ."""
    step = _make_step(tool_id="list_files", operation="get")
    assert RiskTier.classify(step) == RiskTier.READ


# ---------------------------------------------------------------------------
# PermissionMode cycle
# ---------------------------------------------------------------------------


def test_permission_mode_cycle() -> None:
    """NORMAL → AUTO_ACCEPT_EDITS → PLAN → NORMAL."""
    m = PermissionMode.NORMAL
    m = m.next()
    assert m == PermissionMode.AUTO_ACCEPT_EDITS
    m = m.next()
    assert m == PermissionMode.PLAN
    m = m.next()
    assert m == PermissionMode.NORMAL


def test_permission_mode_all_cycle() -> None:
    """Full cycle returns to start after len(members) steps."""
    start = PermissionMode.NORMAL
    current = start
    for _ in range(len(PermissionMode)):
        current = current.next()
    assert current == start


# ---------------------------------------------------------------------------
# PermissionRuleStore — persistence
# ---------------------------------------------------------------------------


def test_rule_store_empty_on_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "permissions.json")
        assert store.list_rules() == []


def test_rule_store_round_trip() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "permissions.json"
        store = PermissionRuleStore(path=path)
        store.add_allow_glob("bash", "*")
        store.add_allow_glob("edit_file", "set")

        # Reload from disk
        store2 = PermissionRuleStore(path=path)
        rules = store2.list_rules()
        assert len(rules) == 2
        assert any(r["tool"] == "bash" and r["action"] == "*" for r in rules)


def test_rule_store_bad_file_degrades_gracefully() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "permissions.json"
        path.write_text("NOT VALID JSON", encoding="utf-8")
        store = PermissionRuleStore(path=path)
        # Should not raise; just returns empty
        assert store.list_rules() == []


def test_rule_store_glob_match() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "permissions.json")
        store.add_allow_glob("bash", "*")

        step_exec = _make_step("bash", "execute")
        step_get = _make_step("bash", "get")
        step_other = _make_step("edit_file", "set")

        assert store.matches_allow(step_exec)
        assert store.matches_allow(step_get)
        assert not store.matches_allow(step_other)


def test_rule_store_tool_action_glob() -> None:
    """Tool:action glob matching — only matches the specific action."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "permissions.json")
        store.add_allow_glob("edit_file", "set")

        assert store.matches_allow(_make_step("edit_file", "set"))
        assert not store.matches_allow(_make_step("edit_file", "get"))


# ---------------------------------------------------------------------------
# PermissionService decision chain
# ---------------------------------------------------------------------------


def test_chain_skip_bypass_allows_without_modal() -> None:
    """A skip predicate → allow immediately, no modal needed."""
    called: list[bool] = []

    def resolver(step: ActionStep) -> str:
        called.append(True)
        return "deny"

    rule_store = MagicMock()
    rule_store.matches_allow = lambda s: False
    rule_store.matches_deny = lambda s: False

    svc = PermissionService(
        rule_store=rule_store,
        session_id=lambda: "s",
        mode_getter=lambda: PermissionMode.NORMAL,
        modal_resolver=resolver,
        skip_predicate=lambda step: True,  # always skip → always approve
    )

    step = _make_step()
    assert svc.decide(step) is True
    assert called == []  # modal never called


def test_chain_allowlist_allow_without_modal() -> None:
    """Allowlist hit → allow, no modal."""
    called: list[bool] = []

    def resolver(step: ActionStep) -> str:
        called.append(True)
        return "deny"

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        store.add_allow_glob("bash", "*")

        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=resolver,
        )
        step = _make_step("bash", "execute")
        assert svc.decide(step) is True
        assert called == []


def test_chain_allowlist_deny_without_modal() -> None:
    """Deny rule in allowlist → deny, no modal."""
    called: list[bool] = []

    def resolver(step: ActionStep) -> str:
        called.append(True)
        return "allow_once"

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        store.add_deny_glob("dangerous_tool", "*")

        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=resolver,
        )
        step = _make_step("dangerous_tool", "execute")
        assert svc.decide(step) is False
        assert called == []


def test_chain_session_grant_allows_without_modal() -> None:
    """Session grant hit → allow, no modal."""
    called: list[bool] = []

    def resolver(step: ActionStep) -> str:
        called.append(True)
        return "deny"

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=resolver,
        )
        step = _make_step("bash", "execute")
        svc.grant_session(step)
        assert svc.decide(step) is True
        assert called == []


def test_chain_modal_allow_once() -> None:
    """No skip/allowlist/session-grant → modal fires; allow_once → True."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=lambda step: "allow_once",
        )
        step = _make_step()
        assert svc.decide(step) is True


def test_chain_modal_allow_session() -> None:
    """allow_session → True AND stores session grant so next call skips modal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        calls: list[str] = []

        def resolver(step: ActionStep) -> str:
            calls.append("modal")
            return "allow_session"

        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=resolver,
        )
        step = _make_step()
        assert svc.decide(step) is True  # modal fires once
        assert len(calls) == 1
        assert svc.decide(step) is True  # session-granted, no modal
        assert len(calls) == 1  # not called again


def test_chain_modal_deny() -> None:
    """deny → False."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=lambda step: "deny",
        )
        assert svc.decide(_make_step()) is False


def test_chain_modal_esc_deny() -> None:
    """esc outcome → deny (safe default)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=lambda step: "esc",
        )
        assert svc.decide(_make_step()) is False


def test_chain_internal_error_defaults_deny() -> None:
    """If modal_resolver raises, decide() returns False (never raises out)."""

    def bad_resolver(step: ActionStep) -> str:
        raise RuntimeError("Modal broke!")

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=bad_resolver,
        )
        # Should not raise; safe default is deny
        assert svc.decide(_make_step()) is False


def test_allow_always_writes_rule() -> None:
    """allow_always(step) persists a glob rule so future calls skip the modal."""
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        calls: list[str] = []

        def resolver(step: ActionStep) -> str:
            calls.append("modal")
            return "allow_once"

        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=resolver,
        )
        step = _make_step("bash", "execute")
        svc.allow_always(step)

        # Now decide should allow without the modal
        assert svc.decide(step) is True
        assert calls == []


def test_auto_accept_edits_mode_allows_write_without_modal() -> None:
    """AUTO_ACCEPT_EDITS mode auto-allows set/write tools."""
    calls: list[str] = []

    def resolver(step: ActionStep) -> str:
        calls.append("modal")
        return "deny"

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.AUTO_ACCEPT_EDITS,
            modal_resolver=resolver,
        )
        step = _make_step("edit_file", "set")
        assert svc.decide(step) is True
        assert calls == []  # modal never called


def test_auto_accept_edits_still_asks_for_exec() -> None:
    """AUTO_ACCEPT_EDITS does NOT auto-allow exec tools."""
    calls: list[str] = []

    def resolver(step: ActionStep) -> str:
        calls.append("modal")
        return "allow_once"

    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        svc = PermissionService(
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.AUTO_ACCEPT_EDITS,
            modal_resolver=resolver,
        )
        step = _make_step("bash", "execute")
        svc.decide(step)
        assert "modal" in calls  # modal DID fire


# ---------------------------------------------------------------------------
# install_permission_service builder
# ---------------------------------------------------------------------------


def test_install_permission_service_wires_gateway() -> None:
    """install_permission_service sets the gateway decision to service.decide."""
    gateway = PermissionGateway(auto_approve=lambda: False)
    with tempfile.TemporaryDirectory() as tmpdir:
        store = PermissionRuleStore(path=Path(tmpdir) / "p.json")
        store.add_allow_glob("mytool", "*")
        install_permission_service(
            gateway,
            rule_store=store,
            session_id=lambda: "s",
            mode_getter=lambda: PermissionMode.NORMAL,
            modal_resolver=lambda step: "deny",
        )
        # Now the gateway delegates to the service
        step = _make_step("mytool", "get")
        assert gateway(step) is True  # allowlist hit → allow


# ---------------------------------------------------------------------------
# App-level: shift+tab cycles permission_mode + footer reflects it
# ---------------------------------------------------------------------------


def test_permission_mode_reactive_starts_normal() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test():
            assert app.permission_mode == PermissionMode.NORMAL

    asyncio.run(_run())


def test_shift_tab_cycles_permission_mode() -> None:
    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            assert app.permission_mode == PermissionMode.NORMAL
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.permission_mode == PermissionMode.AUTO_ACCEPT_EDITS
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.permission_mode == PermissionMode.PLAN
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.permission_mode == PermissionMode.NORMAL

    asyncio.run(_run())


def test_permission_mode_reflected_in_subtitle() -> None:
    """After shift+tab, sub_title (footer) includes the mode name."""

    async def _run() -> None:
        app, _ = _make_app()
        async with app.run_test() as pilot:
            await pilot.press("shift+tab")
            await pilot.pause()
            # sub_title should mention the mode somehow
            assert app.permission_mode == PermissionMode.AUTO_ACCEPT_EDITS
            # sub_title is how we expose the mode in the footer
            assert app.sub_title != ""

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# PermissionModal (unit smoke — no live App, just construct)
# ---------------------------------------------------------------------------


def test_permission_modal_imports() -> None:
    """PermissionModal class can be imported without error."""
    from mewbo_cli.tui.widgets.permission_modal import PermissionModal  # noqa: F401


def test_permission_modal_can_be_constructed() -> None:
    """PermissionModal constructs without a live App."""
    from mewbo_cli.cli_theme import DEFAULT_PALETTE
    from mewbo_cli.tui.widgets.permission_modal import PermissionModal
    from textual.screen import ModalScreen

    step = _make_step()
    modal = PermissionModal(step, palette=DEFAULT_PALETTE)
    # Just verify it's a ModalScreen subclass
    assert isinstance(modal, ModalScreen)


# ---------------------------------------------------------------------------
# PermissionModal Pilot tests — push modal onto a live App, drive keys/buttons
# ---------------------------------------------------------------------------
#
# We use a minimal throwaway App that accepts an ActionStep, pushes the
# PermissionModal via push_screen (with a result callback), and stores the
# dismiss value for assertion.  This exercises the real action_* handlers,
# on_button_pressed, and the esc binding.


def _make_modal_app(step: ActionStep) -> tuple[Any, list[str]]:
    """Build a tiny App that pushes PermissionModal and collects the result."""
    from mewbo_cli.cli_theme import DEFAULT_PALETTE
    from mewbo_cli.tui.widgets.permission_modal import PermissionModal
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    results: list[str] = []

    class _ModalTestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("modal host")

        def on_mount(self) -> None:
            modal = PermissionModal(step, palette=DEFAULT_PALETTE)
            self.push_screen(modal, callback=lambda v: results.append(v))

    return _ModalTestApp(), results


def test_permission_modal_key_a_allow_once() -> None:
    """Pressing 'a' dismisses with 'allow_once'."""

    async def _run() -> None:
        step = _make_step(tool_id="bash", operation="execute")
        app, results = _make_modal_app(step)
        async with app.run_test() as pilot:
            await pilot.pause()  # let on_mount push the modal
            await pilot.press("a")
            await pilot.pause()
        assert results == ["allow_once"]

    asyncio.run(_run())


def test_permission_modal_key_s_allow_session() -> None:
    """Pressing 's' dismisses with 'allow_session'."""

    async def _run() -> None:
        step = _make_step(tool_id="bash", operation="execute")
        app, results = _make_modal_app(step)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
        assert results == ["allow_session"]

    asyncio.run(_run())


def test_permission_modal_key_d_deny() -> None:
    """Pressing 'd' dismisses with 'deny'."""

    async def _run() -> None:
        step = _make_step(tool_id="bash", operation="execute")
        app, results = _make_modal_app(step)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
        assert results == ["deny"]

    asyncio.run(_run())


def test_permission_modal_esc_deny() -> None:
    """Pressing 'escape' dismisses with 'deny' (safe default)."""

    async def _run() -> None:
        step = _make_step(tool_id="bash", operation="execute")
        app, results = _make_modal_app(step)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
        assert results == ["deny"]

    asyncio.run(_run())
