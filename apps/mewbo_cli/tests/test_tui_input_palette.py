#!/usr/bin/env python3
"""Tests for the palette provider + make_input_installer (issue #155, epic #149).

The installer is exercised against the real ``MewboApp`` (imported read-only) —
the same integration path the controller wires in ``cli_master._build_installers``.
"""

from __future__ import annotations

import asyncio
from typing import Any

from mewbo_cli.tui.app import MewboApp
from mewbo_cli.tui.input.completion import CompletionEngine
from mewbo_cli.tui.input.custom_commands import CustomCommand
from mewbo_cli.tui.input.palette import (
    InputContext,
    MewboCommandProvider,
    make_input_installer,
)
from mewbo_cli.tui.seams import (
    InputGateway,
    MessageRendererRegistry,
    PermissionGateway,
    SidebarSlotRegistry,
)
from mewbo_cli.tui.widgets.header import HeaderContext
from mewbo_cli.tui.widgets.input_area import InputArea


def _header_ctx() -> HeaderContext:
    return HeaderContext(
        title="Mewbo",
        version="0.0.13",
        status_label="Ready",
        status_color="green",
        model="openai/gpt-5.2",
        session_id="sess-123",
        base_url="http://localhost:4000",
        langfuse_enabled=False,
        langfuse_reason=None,
        builtin_enabled=1,
        builtin_disabled=0,
        external_enabled=0,
        external_disabled=0,
        skill_count=0,
    )


class _FakeEngine:
    def __init__(self, emit: Any, emit_renderable: Any) -> None:
        self.emit = emit
        self.emit_renderable = emit_renderable

    def handle(self, text: str) -> bool:
        return True


class _Commands:
    def list_commands(self) -> list[str]:
        return ["/help", "/plan", "/skills"]


class _Skill:
    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description


class _Skills:
    def list_user_invocable(self) -> list[_Skill]:
        return [_Skill("review", "Review code")]


def _make_app(installer) -> MewboApp:
    return MewboApp(
        header_ctx=_header_ctx(),
        messages=MessageRendererRegistry(),
        sidebar_slots=SidebarSlotRegistry(),
        permission=PermissionGateway(auto_approve=lambda: True),
        input_gateway=InputGateway(),
        engine_factory=lambda e, r: _FakeEngine(e, r),
        installers=[installer],
    )


# --- InputContext merge ---------------------------------------------------


def test_input_context_merges_all_sources():
    ctx = InputContext(
        command_names=lambda: ["/help", "/plan"],
        skill_candidates=lambda: [("review", "Review code")],
        custom_commands=lambda: [
            CustomCommand("frontend:c", "scaffold", "<n>", "body", (), "project")
        ],
        mcp_prompts=lambda: [("summarize", "MCP summarize")],
    )
    names = {c.name for c in ctx.command_candidates()}
    assert names == {"help", "plan", "review", "frontend:c", "summarize"}
    # the custom command's argument hint survives the merge
    hint = {c.name: c.argument_hint for c in ctx.command_candidates()}["frontend:c"]
    assert hint == "<n>"


def test_input_context_dedupes_by_name():
    ctx = InputContext(
        command_names=lambda: ["/dup"],
        skill_candidates=lambda: [("dup", "skill dup")],
        custom_commands=lambda: [],
    )
    cands = ctx.command_candidates()
    assert [c.name for c in cands].count("dup") == 1
    # command wins over skill on a clash (added first)
    assert cands[0].kind == "command"


def test_input_context_source_error_degrades():
    def boom():
        raise RuntimeError("x")

    ctx = InputContext(
        command_names=boom,
        skill_candidates=lambda: [("ok", "")],
        custom_commands=boom,
    )
    assert {c.name for c in ctx.command_candidates()} == {"ok"}


# --- installer wiring -----------------------------------------------------


def test_installer_injects_engine_and_registers_provider(tmp_path):
    async def _run() -> None:
        installer = make_input_installer(
            command_registry=_Commands(),
            skill_registry=_Skills(),
            cwd_provider=lambda: str(tmp_path),
        )
        app = _make_app(installer)
        async with app.run_test():
            widget = app.query_one(InputArea)
            # engine injected → completion now works through the widget
            assert isinstance(widget._engine, CompletionEngine)
            results = widget._engine.complete("/he", cursor=3)
            assert any(r.display == "/help" for r in results)
            # provider registered on the live (instance) COMMANDS set
            assert MewboCommandProvider in app.COMMANDS
            # class-level set is untouched (no global mutation)
            assert MewboCommandProvider not in type(app).COMMANDS

    asyncio.run(_run())


def test_installer_command_completion_includes_skill(tmp_path):
    async def _run() -> None:
        installer = make_input_installer(
            command_registry=_Commands(),
            skill_registry=_Skills(),
            cwd_provider=lambda: str(tmp_path),
        )
        app = _make_app(installer)
        async with app.run_test():
            widget = app.query_one(InputArea)
            results = widget._engine.complete("/rev", cursor=4)
            assert any(r.display == "/review" for r in results)

    asyncio.run(_run())
