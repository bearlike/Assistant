"""Tests for the native LSP integration."""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

from mewbo_tools.integration.lsp import LSP_AVAILABLE
from mewbo_tools.integration.lsp.servers import (
    BUILTIN_SERVERS,
    ServerDef,
    available_servers,
)

# ------------------------------------------------------------------
# servers.py
# ------------------------------------------------------------------


class TestServerDef:
    def test_builtin_servers_defined(self):
        assert len(BUILTIN_SERVERS) >= 4
        ids = {s.id for s in BUILTIN_SERVERS}
        assert "pyright" in ids
        assert "typescript-language-server" in ids
        assert "gopls" in ids
        assert "rust-analyzer" in ids

    def test_server_def_frozen(self):
        s = BUILTIN_SERVERS[0]
        try:
            s.id = "other"  # type: ignore[misc]
            assert False, "Should be frozen"
        except AttributeError:
            pass

    def test_available_servers_filters_by_binary(self):
        """Only servers whose command binary is on PATH are returned."""
        with patch.object(shutil, "which", return_value=None):
            result = available_servers()
        assert result == []

    def test_available_servers_returns_installed(self):
        """Servers whose binary is found on PATH are returned."""

        def fake_which(cmd: str) -> str | None:
            return "/usr/bin/pyright-langserver" if "pyright" in cmd else None

        with patch.object(shutil, "which", side_effect=fake_which):
            result = available_servers()
        assert len(result) == 1
        assert result[0].id == "pyright"

    def test_available_servers_with_overrides_disable(self):
        """Overrides can disable built-in servers."""

        def fake_which(cmd: str) -> str | None:
            return f"/usr/bin/{cmd}"

        with patch.object(shutil, "which", side_effect=fake_which):
            result = available_servers({"pyright": {"disabled": True}})
        ids = {s.id for s in result}
        assert "pyright" not in ids

    def test_available_servers_with_custom_server(self):
        """User-defined servers are included if binary is found."""

        def fake_which(cmd: str) -> str | None:
            return f"/usr/bin/{cmd}" if cmd == "my-lsp" else None

        with patch.object(shutil, "which", side_effect=fake_which):
            result = available_servers(
                {
                    "my-lang": {
                        "command": ["my-lsp", "--stdio"],
                        "extensions": [".xyz"],
                        "language_id": "xyzlang",
                    }
                }
            )
        assert len(result) == 1
        assert result[0].id == "my-lang"
        assert result[0].extensions == (".xyz",)


# ------------------------------------------------------------------
# __init__.py
# ------------------------------------------------------------------


class TestLSPAvailability:
    def test_lsp_available_when_pygls_installed(self):
        """pygls is installed in dev deps, so LSP_AVAILABLE should be True."""
        assert LSP_AVAILABLE is True


# ------------------------------------------------------------------
# manager.py
# ------------------------------------------------------------------


class TestLSPServerManager:
    def test_manager_creates_extension_map(self):
        """Manager builds extension → server map from available servers."""
        from mewbo_tools.integration.lsp.manager import LSPServerManager

        # Patch available_servers to return a fake server
        fake_server = ServerDef(
            id="test-server",
            extensions=(".py", ".pyi"),
            command=("test-langserver", "--stdio"),
            root_markers=("pyproject.toml",),
            language_id="python",
        )
        with patch(
            "mewbo_tools.integration.lsp.manager.available_servers",
            return_value=[fake_server],
        ):
            mgr = LSPServerManager(cwd="/tmp")

        assert mgr.server_for_file("test.py") is not None
        assert mgr.server_for_file("test.py").id == "test-server"
        assert mgr.server_for_file("test.go") is None

    def test_manager_no_servers_available(self):
        """Manager handles zero available servers gracefully."""
        from mewbo_tools.integration.lsp.manager import LSPServerManager

        with patch(
            "mewbo_tools.integration.lsp.manager.available_servers",
            return_value=[],
        ):
            mgr = LSPServerManager(cwd="/tmp")

        assert mgr.server_for_file("test.py") is None

    def test_find_root_walks_up(self, tmp_path: Path):
        """_find_root walks up to find a root marker file."""
        from mewbo_tools.integration.lsp.manager import LSPServerManager

        # Create structure: tmp_path/pyproject.toml, tmp_path/src/pkg/
        (tmp_path / "pyproject.toml").touch()
        (tmp_path / "src" / "pkg").mkdir(parents=True)

        with patch(
            "mewbo_tools.integration.lsp.manager.available_servers",
            return_value=[],
        ):
            mgr = LSPServerManager(cwd=str(tmp_path / "src" / "pkg"))

        server_def = ServerDef(
            id="test",
            extensions=(".py",),
            command=("test",),
            root_markers=("pyproject.toml",),
            language_id="python",
        )
        root = mgr._find_root(server_def)
        assert root == str(tmp_path)

    def test_cached_diagnostics_empty(self):
        """Cached diagnostics returns empty list for unknown files."""
        from mewbo_tools.integration.lsp.manager import LSPServerManager

        with patch(
            "mewbo_tools.integration.lsp.manager.available_servers",
            return_value=[],
        ):
            mgr = LSPServerManager(cwd="/tmp")

        assert mgr.get_cached_diagnostics("/tmp/test.py") == []


# ------------------------------------------------------------------
# tool.py
# ------------------------------------------------------------------


class TestLSPToolFormatting:
    def test_format_diagnostics_empty(self):
        """Empty diagnostics produce a 'clean' message."""
        from unittest.mock import MagicMock

        from mewbo_tools.integration.lsp.tool import LSPTool

        tool = LSPTool.__new__(LSPTool)
        manager = MagicMock()
        manager.server_for_file.return_value = MagicMock(id="pyright")

        result = tool._format_diagnostics("/tmp/test.py", [], manager)
        assert "clean" in result.lower() or "no diagnostics" in result.lower()

    def test_format_locations_none(self):
        """None locations produce 'not found' message."""
        from mewbo_tools.integration.lsp.tool import LSPTool

        tool = LSPTool.__new__(LSPTool)
        result = tool._format_locations(None, "Definition")
        assert "not found" in result.lower() or "no definition" in result.lower()

    def test_format_hover_none(self):
        """None hover produces 'not available' message."""
        from mewbo_tools.integration.lsp.tool import LSPTool

        tool = LSPTool.__new__(LSPTool)
        result = tool._format_hover(None)
        assert "no hover" in result.lower() or "not available" in result.lower()

    def test_format_hover_markup_content(self):
        """MarkupContent hover returns the value."""
        from lsprotocol import types
        from mewbo_tools.integration.lsp.tool import LSPTool

        tool = LSPTool.__new__(LSPTool)
        hover = types.Hover(
            contents=types.MarkupContent(
                kind=types.MarkupKind.Markdown,
                value="```python\ndef foo() -> int\n```",
            )
        )
        result = tool._format_hover(hover)
        assert "def foo" in result

    def test_format_diagnostics_filters_hints(self):
        """Only errors and warnings are shown; hints are suppressed."""
        from lsprotocol import types
        from mewbo_tools.integration.lsp.tool import LSPTool

        tool = LSPTool.__new__(LSPTool)
        diags = [
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=10, character=0),
                    end=types.Position(line=10, character=5),
                ),
                severity=types.DiagnosticSeverity.Error,
                message="Type error here",
            ),
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=20, character=0),
                    end=types.Position(line=20, character=5),
                ),
                severity=types.DiagnosticSeverity.Hint,
                message="Hint: consider refactoring",
            ),
        ]
        from unittest.mock import MagicMock

        manager = MagicMock()
        manager.server_for_file.return_value = MagicMock(id="pyright")

        result = tool._format_diagnostics("/tmp/test.py", diags, manager)
        assert "Type error here" in result
        assert "Hint: consider refactoring" not in result
        assert "1 error" in result


# ------------------------------------------------------------------
# tool_registry integration
# ------------------------------------------------------------------


class TestLSPToolRegistration:
    def test_lsp_tool_in_default_registry(self, monkeypatch):
        """lsp_tool is registered in the default tool registry."""
        monkeypatch.setenv("MEWBO_HOME", "/tmp/mewbo-test-lsp")
        from mewbo_core.config import reset_config

        reset_config()

        from mewbo_core.tool_registry import _default_registry

        registry = _default_registry()
        specs = registry.list_specs(include_disabled=True)
        tool_ids = {s.tool_id for s in specs}
        assert "lsp_tool" in tool_ids

    def test_lsp_tool_is_read_only(self, monkeypatch):
        """lsp_tool is read-only (safe for plan mode)."""
        monkeypatch.setenv("MEWBO_HOME", "/tmp/mewbo-test-lsp")
        from mewbo_core.config import reset_config

        reset_config()

        from mewbo_core.tool_registry import _default_registry

        registry = _default_registry()
        specs = {s.tool_id: s for s in registry.list_specs(include_disabled=True)}
        assert specs["lsp_tool"].read_only is True
        assert specs["lsp_tool"].concurrency_safe is True
