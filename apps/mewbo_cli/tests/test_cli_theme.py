"""Tests for cli_theme: Palette, build_theme, ThemeManager, detect_terminal_is_dark."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mewbo_cli.cli_theme import (
    DEFAULT_PALETTE,
    HYPER_PALETTE,
    LIGHT_PALETTE,
    ROLE_NAMES,
    Palette,
    ThemeManager,
    auto_palette,
    build_theme,
    detect_terminal_is_dark,
)

# ---------------------------------------------------------------------------
# ROLE_NAMES and Palette field contract
# ---------------------------------------------------------------------------


def test_role_names_count() -> None:
    assert len(ROLE_NAMES) == 15


def test_role_names_exact_order() -> None:
    assert list(ROLE_NAMES) == [
        "primary",
        "secondary",
        "accent",
        "fg_base",
        "bg_base",
        "muted",
        "border",
        "error",
        "warning",
        "success",
        "user",
        "assistant",
        "diff_add",
        "diff_del",
        "diff_eq",
    ]


def test_palette_fields_match_role_names() -> None:
    """Palette must have exactly the 15 fields listed in ROLE_NAMES."""
    import dataclasses

    field_names = [f.name for f in dataclasses.fields(Palette)]
    assert field_names == list(ROLE_NAMES)


def test_palette_is_frozen() -> None:
    """Palette must be immutable (frozen dataclass)."""
    params = getattr(DEFAULT_PALETTE, "__dataclass_params__", None)
    assert params is not None and params.frozen


def test_default_palette_all_hex() -> None:
    import dataclasses

    for f in dataclasses.fields(DEFAULT_PALETTE):
        val = getattr(DEFAULT_PALETTE, f.name)
        assert isinstance(val, str), f"{f.name} is not a string"
        assert val.startswith("#") and len(val) in {4, 7, 9}, (
            f"{f.name}={val!r} is not a hex color"
        )


def test_hyper_palette_all_hex() -> None:
    import dataclasses

    for f in dataclasses.fields(HYPER_PALETTE):
        val = getattr(HYPER_PALETTE, f.name)
        assert val.startswith("#") and len(val) in {4, 7, 9}


def test_light_palette_all_hex() -> None:
    import dataclasses

    for f in dataclasses.fields(LIGHT_PALETTE):
        val = getattr(LIGHT_PALETTE, f.name)
        assert val.startswith("#") and len(val) in {4, 7, 9}


# ---------------------------------------------------------------------------
# build_theme
# ---------------------------------------------------------------------------


def test_build_theme_name_and_dark() -> None:
    theme = build_theme(DEFAULT_PALETTE, name="test-dark")
    assert theme.name == "test-dark"
    assert theme.dark is True


def test_build_theme_light_flag() -> None:
    theme = build_theme(LIGHT_PALETTE, name="test-light", dark=False)
    assert theme.dark is False


def test_build_theme_native_fields() -> None:
    p = DEFAULT_PALETTE
    theme = build_theme(p, name="check")
    assert theme.primary == p.primary
    assert theme.secondary == p.secondary
    assert theme.accent == p.accent
    assert theme.error == p.error
    assert theme.warning == p.warning
    assert theme.success == p.success
    assert theme.foreground == p.fg_base
    assert theme.background == p.bg_base


def test_build_theme_variables_present() -> None:
    theme = build_theme(DEFAULT_PALETTE, name="check")
    assert theme.variables is not None
    # These must be in variables with exact underscore keys
    for key in ("muted", "border", "user", "assistant", "diff_add", "diff_del", "diff_eq"):
        assert key in theme.variables, f"missing key {key!r} in variables"


def test_build_theme_diff_add_value() -> None:
    theme = build_theme(DEFAULT_PALETTE, name="check")
    assert theme.variables["diff_add"] == DEFAULT_PALETTE.diff_add


def test_build_theme_diff_del_value() -> None:
    theme = build_theme(DEFAULT_PALETTE, name="check")
    assert theme.variables["diff_del"] == DEFAULT_PALETTE.diff_del


def test_build_theme_diff_eq_value() -> None:
    theme = build_theme(DEFAULT_PALETTE, name="check")
    assert theme.variables["diff_eq"] == DEFAULT_PALETTE.diff_eq


# ---------------------------------------------------------------------------
# Textual CSS variable integration (async via asyncio.run in sync wrapper)
# ---------------------------------------------------------------------------


def test_css_vars_registered_in_textual_app() -> None:
    """Theme variables must be accessible via app.get_css_variables()."""
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class _TinyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("hi")

    async def _run() -> None:
        theme = build_theme(DEFAULT_PALETTE, name="mewbo-test")
        app = _TinyApp()
        async with app.run_test():
            app.register_theme(theme)
            app.theme = "mewbo-test"
            css_vars = app.get_css_variables()
            assert css_vars.get("diff_add") == DEFAULT_PALETTE.diff_add
            assert css_vars.get("diff_del") == DEFAULT_PALETTE.diff_del
            assert css_vars.get("diff_eq") == DEFAULT_PALETTE.diff_eq
            assert css_vars.get("muted") == DEFAULT_PALETTE.muted
            assert css_vars.get("border") == DEFAULT_PALETTE.border
            assert css_vars.get("user") == DEFAULT_PALETTE.user
            assert css_vars.get("assistant") == DEFAULT_PALETTE.assistant

    asyncio.run(_run())


def test_all_15_roles_css_vars_present() -> None:
    """All variable-dict roles should be accessible as CSS vars after registering."""
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class _TinyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("hi")

    async def _run() -> None:
        theme = build_theme(DEFAULT_PALETTE, name="mewbo-full")
        app = _TinyApp()
        async with app.run_test():
            app.register_theme(theme)
            app.theme = "mewbo-full"
            css_vars = app.get_css_variables()
            # All 15 contract roles must surface as CSS vars ($fg_base/$bg_base
            # included). Native semantic fields (primary/secondary/accent/error/
            # warning/success) are processed by Textual's colour system for shade
            # generation, so only presence is guaranteed for those; the custom
            # variables-dict roles pass through verbatim and must match exactly.
            verbatim_roles = {
                "fg_base", "bg_base", "muted", "border",
                "user", "assistant", "diff_add", "diff_del", "diff_eq",
            }
            for role in ROLE_NAMES:
                assert role in css_vars, f"CSS var ${role} not found"
                if role in verbatim_roles:
                    assert css_vars[role].lower() == getattr(DEFAULT_PALETTE, role).lower(), (
                        f"${role} mismatch"
                    )

    asyncio.run(_run())


def test_native_semantic_roles_exposed_as_css_vars() -> None:
    """Native semantic roles surface as $primary/$error/etc (Textual-themed)."""
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class _TinyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("hi")

    async def _run() -> None:
        theme = build_theme(DEFAULT_PALETTE, name="mewbo-native")
        app = _TinyApp()
        async with app.run_test():
            app.register_theme(theme)
            app.theme = "mewbo-native"
            css_vars = app.get_css_variables()
            for role in ("primary", "secondary", "accent", "error", "warning", "success"):
                assert role in css_vars, f"CSS var ${role} not found"

    asyncio.run(_run())


def test_theme_switch_changes_active_theme() -> None:
    """ThemeManager.switch() must change app.theme."""
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class _TinyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("hi")

    async def _run() -> None:
        dark_theme = build_theme(DEFAULT_PALETTE, name="mewbo-dark")
        hyper_theme = build_theme(HYPER_PALETTE, name="mewbo-hyper")
        app = _TinyApp()
        async with app.run_test():
            mgr = ThemeManager(themes=[dark_theme, hyper_theme])
            mgr.register_all(app)
            app.theme = "mewbo-dark"
            assert app.theme == "mewbo-dark"
            result = mgr.switch(app, "mewbo-hyper")
            assert result is True
            assert app.theme == "mewbo-hyper"

    asyncio.run(_run())


def test_theme_switch_unknown_returns_false() -> None:
    async def _run() -> None:
        from textual.app import App, ComposeResult
        from textual.widgets import Label

        class _TinyApp(App[None]):
            def compose(self) -> ComposeResult:
                yield Label("hi")

        app = _TinyApp()
        mgr = ThemeManager(themes=[])
        async with app.run_test():
            result = mgr.switch(app, "nonexistent-theme")
            assert result is False

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# ThemeManager (sync tests)
# ---------------------------------------------------------------------------


def test_theme_manager_list_themes_sorted() -> None:
    t1 = build_theme(DEFAULT_PALETTE, name="beta")
    t2 = build_theme(DEFAULT_PALETTE, name="alpha")
    mgr = ThemeManager(themes=[t1, t2])
    assert mgr.list_themes() == ["alpha", "beta"]


def test_theme_manager_load_json_themes_valid(tmp_path: Path) -> None:
    theme_file = tmp_path / "custom.json"
    palette_data = {f: "#aabbcc" for f in ROLE_NAMES}
    theme_file.write_text(
        json.dumps({"name": "custom-dark", "dark": True, "palette": palette_data})
    )
    mgr = ThemeManager(themes=[], themes_dir=tmp_path)
    mgr.load_json_themes()
    assert "custom-dark" in mgr.list_themes()


def test_theme_manager_load_json_themes_bad_file_skipped(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("not json at all }{")
    mgr = ThemeManager(themes=[], themes_dir=tmp_path)
    # Should not raise, should skip silently
    mgr.load_json_themes()
    assert mgr.list_themes() == []


def test_theme_manager_load_json_themes_missing_role_skipped(tmp_path: Path) -> None:
    theme_file = tmp_path / "partial.json"
    partial_palette = {"primary": "#aabbcc"}  # missing 14 roles
    theme_file.write_text(
        json.dumps({"name": "partial", "dark": True, "palette": partial_palette})
    )
    mgr = ThemeManager(themes=[], themes_dir=tmp_path)
    mgr.load_json_themes()
    assert "partial" not in mgr.list_themes()


def test_theme_manager_load_json_themes_nonexistent_dir() -> None:
    mgr = ThemeManager(themes=[], themes_dir=Path("/nonexistent/path/xyz"))
    # Should not raise when directory doesn't exist
    mgr.load_json_themes()
    assert mgr.list_themes() == []


def test_theme_manager_apply_command_no_arg() -> None:
    t = build_theme(DEFAULT_PALETTE, name="mewbo-dark")
    mgr = ThemeManager(themes=[t])
    result = mgr.apply_command(None, None)
    assert "mewbo-dark" in result


def test_theme_manager_apply_command_list() -> None:
    t = build_theme(DEFAULT_PALETTE, name="mewbo-dark")
    mgr = ThemeManager(themes=[t])
    result = mgr.apply_command(None, "list")
    assert "mewbo-dark" in result


def test_theme_manager_apply_command_unknown() -> None:
    mgr = ThemeManager(themes=[])
    result = mgr.apply_command(None, "does-not-exist")
    assert "unknown" in result.lower() or "not found" in result.lower()


# ---------------------------------------------------------------------------
# detect_terminal_is_dark
# ---------------------------------------------------------------------------


def test_detect_dark_from_colorfgbg_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORFGBG", "15;0")
    assert detect_terminal_is_dark() is True


def test_detect_dark_from_colorfgbg_light(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORFGBG", "0;15")
    assert detect_terminal_is_dark() is False


def test_detect_dark_unknown_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert detect_terminal_is_dark() is True


def test_detect_dark_malformed_env_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORFGBG", "notanumber")
    assert detect_terminal_is_dark() is True


# ---------------------------------------------------------------------------
# auto_palette
# ---------------------------------------------------------------------------


def test_auto_palette_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORFGBG", "15;0")
    p = auto_palette()
    assert p is DEFAULT_PALETTE


def test_auto_palette_light(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLORFGBG", "0;15")
    p = auto_palette()
    assert p is LIGHT_PALETTE


# ---------------------------------------------------------------------------
# apply_command honest behaviour (no-app + live-app)
# ---------------------------------------------------------------------------


def test_apply_command_live_app_switches_theme() -> None:
    """apply_command with a live app must switch and confirm via the message."""
    from textual.app import App, ComposeResult
    from textual.widgets import Label

    class _TinyApp(App[None]):
        def compose(self) -> ComposeResult:
            yield Label("hi")

    async def _run() -> None:
        dark_theme = build_theme(DEFAULT_PALETTE, name="mewbo-dark")
        hyper_theme = build_theme(HYPER_PALETTE, name="mewbo-hyper")
        mgr = ThemeManager(themes=[dark_theme, hyper_theme])
        app = _TinyApp()
        async with app.run_test():
            mgr.register_all(app)
            app.theme = "mewbo-dark"
            msg = mgr.apply_command(app, "mewbo-hyper")
            assert "switched" in msg.lower()
            assert "mewbo-hyper" in msg
            assert app.theme == "mewbo-hyper"

    asyncio.run(_run())


def test_apply_command_no_app_known_theme_does_not_claim_switch() -> None:
    """apply_command(None, known) must NOT say 'switched'; returns 'selected' message."""
    dark_theme = build_theme(DEFAULT_PALETTE, name="mewbo-dark")
    mgr = ThemeManager(themes=[dark_theme])
    msg = mgr.apply_command(None, "mewbo-dark")
    assert "switched" not in msg.lower()
    assert "selected" in msg.lower() or "no active app" in msg.lower()
    assert "mewbo-dark" in msg


def test_apply_command_no_app_unknown_theme_returns_error() -> None:
    """apply_command(None, unknown) must return the unknown-theme error."""
    mgr = ThemeManager(themes=[])
    msg = mgr.apply_command(None, "nonexistent")
    assert "unknown" in msg.lower() or "not found" in msg.lower()
