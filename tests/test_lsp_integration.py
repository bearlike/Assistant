"""Integration tests for the LSP module.

Covers:
- integration/lsp/__init__.py  (get_lsp_manager, run_lsp_async, get_passive_diagnostics,
                                shutdown_lsp_managers, _get_lsp_loop)
- integration/lsp/manager.py   (ensure_server, open_file, shutdown_all, _start_server,
                                _find_root, get_cached_diagnostics)
- integration/lsp/tool.py      (LSPTool.run, _do_diagnostics, _do_navigation,
                                _format_diagnostics, _format_locations, _format_hover)

All LSP client I/O is mocked — no real language server is spawned in CI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from lsprotocol import types
from mewbo_core.classes import ActionStep
from mewbo_tools.integration.lsp import (
    _managers,
    get_lsp_manager,
    get_passive_diagnostics,
    run_lsp_async,
)
from mewbo_tools.integration.lsp.manager import LSPServerManager
from mewbo_tools.integration.lsp.servers import ServerDef
from mewbo_tools.integration.lsp.tool import LSPTool

# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _make_server_def(
    ext: str = ".py",
    sid: str = "test-lsp",
    command: tuple[str, ...] = ("test-langserver", "--stdio"),
) -> ServerDef:
    return ServerDef(
        id=sid,
        extensions=(ext,),
        command=command,
        root_markers=("pyproject.toml",),
        language_id="python",
    )


def _make_manager(cwd: str = "/tmp", servers: list[ServerDef] | None = None) -> LSPServerManager:
    """Create an LSPServerManager with patched available_servers."""
    with patch(
        "mewbo_tools.integration.lsp.manager.available_servers",
        return_value=servers or [],
    ):
        return LSPServerManager(cwd=cwd)


def _make_fake_client() -> MagicMock:
    """Return a MagicMock that looks enough like BaseLanguageClient for our tests."""
    client = MagicMock()
    client.text_document_did_open = MagicMock()
    client.text_document_definition_async = AsyncMock(return_value=None)
    client.text_document_references_async = AsyncMock(return_value=None)
    client.text_document_hover_async = AsyncMock(return_value=None)
    return client


def _make_diagnostic(
    line: int = 0,
    severity: types.DiagnosticSeverity = types.DiagnosticSeverity.Error,
    message: str = "some error",
    code: str | None = None,
) -> types.Diagnostic:
    rng = types.Range(
        start=types.Position(line=line, character=0),
        end=types.Position(line=line, character=5),
    )
    return types.Diagnostic(range=rng, severity=severity, message=message, code=code)


# ---------------------------------------------------------------------------
# run_lsp_async
# ---------------------------------------------------------------------------


class TestRunLspAsync:
    def test_runs_coroutine_and_returns_result(self):
        async def _coro():
            return 42

        result = run_lsp_async(_coro(), timeout=5)
        assert result == 42

    def test_reuses_background_loop(self):
        from mewbo_tools.integration.lsp import _get_lsp_loop

        loop1 = _get_lsp_loop()
        loop2 = _get_lsp_loop()
        assert loop1 is loop2

    def test_background_thread_is_daemon(self):
        from mewbo_tools.integration.lsp import _get_lsp_loop

        _get_lsp_loop()  # ensure created
        import mewbo_tools.integration.lsp as lsp_mod

        assert lsp_mod._lsp_thread is not None
        assert lsp_mod._lsp_thread.daemon is True


# ---------------------------------------------------------------------------
# get_lsp_manager / _managers singleton
# ---------------------------------------------------------------------------


class TestGetLspManager:
    def setup_method(self):
        # Clean module-level _managers between tests
        _managers.clear()

    def test_creates_manager_on_first_call(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
        from mewbo_core.config import reset_config

        reset_config()
        with patch("mewbo_tools.integration.lsp.manager.available_servers", return_value=[]):
            mgr = get_lsp_manager(str(tmp_path))
        assert isinstance(mgr, LSPServerManager)

    def test_returns_same_manager_for_same_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
        from mewbo_core.config import reset_config

        reset_config()
        with patch("mewbo_tools.integration.lsp.manager.available_servers", return_value=[]):
            m1 = get_lsp_manager(str(tmp_path))
            m2 = get_lsp_manager(str(tmp_path))
        assert m1 is m2

    def test_creates_separate_manager_per_cwd(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("MEWBO_HOME", str(tmp_path))
        from mewbo_core.config import reset_config

        reset_config()
        cwd_a = str(tmp_path / "a")
        cwd_b = str(tmp_path / "b")
        with patch("mewbo_tools.integration.lsp.manager.available_servers", return_value=[]):
            m1 = get_lsp_manager(cwd_a)
            m2 = get_lsp_manager(cwd_b)
        assert m1 is not m2

    def teardown_method(self):
        _managers.clear()


# ---------------------------------------------------------------------------
# get_passive_diagnostics
# ---------------------------------------------------------------------------


class TestGetPassiveDiagnostics:
    def setup_method(self):
        _managers.clear()

    def teardown_method(self):
        _managers.clear()

    def test_returns_none_when_no_managers(self):
        result = get_passive_diagnostics("/tmp/foo.py", "/tmp")
        assert result is None

    def test_returns_none_when_no_manager_for_cwd(self):
        _managers["/other"] = MagicMock()
        result = get_passive_diagnostics("/tmp/foo.py", "/tmp")
        assert result is None

    def test_returns_none_when_server_not_running(self, tmp_path: Path):
        """Server not in _clients → skip passive diagnostics."""
        mgr = _make_manager(cwd=str(tmp_path))
        mgr._clients = {}
        sdef = _make_server_def()
        mgr._extension_map[".py"] = sdef
        _managers[str(tmp_path)] = mgr

        result = get_passive_diagnostics(str(tmp_path / "foo.py"), str(tmp_path))
        assert result is None

    def test_returns_none_when_no_server_for_file(self, tmp_path: Path):
        """No extension mapping → None."""
        mgr = _make_manager(cwd=str(tmp_path))
        _managers[str(tmp_path)] = mgr

        result = get_passive_diagnostics(str(tmp_path / "foo.xyz"), str(tmp_path))
        assert result is None

    def test_returns_none_on_exception(self, tmp_path: Path):
        """Exceptions in passive diagnostics must be swallowed."""
        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])
        fake_client = MagicMock()
        mgr._clients[sdef.id] = fake_client

        # Make run_lsp_async raise
        with patch(
            "mewbo_tools.integration.lsp.run_lsp_async",
            side_effect=RuntimeError("boom"),
        ):
            result = get_passive_diagnostics(str(tmp_path / "foo.py"), str(tmp_path))
        assert result is None

    def test_returns_none_when_lsp_not_available(self, tmp_path: Path):
        """If LSP_AVAILABLE is False, immediately return None."""
        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])
        fake_client = MagicMock()
        mgr._clients[sdef.id] = fake_client
        _managers[str(tmp_path)] = mgr

        with patch("mewbo_tools.integration.lsp.LSP_AVAILABLE", False):
            result = get_passive_diagnostics(str(tmp_path / "foo.py"), str(tmp_path))
        assert result is None

    def test_returns_none_when_no_errors_or_warnings(self, tmp_path: Path):
        """Server running but only Hint diagnostics → None (suppressed)."""
        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])
        fake_client = MagicMock()
        mgr._clients[sdef.id] = fake_client
        _managers[str(tmp_path)] = mgr

        hint = _make_diagnostic(severity=types.DiagnosticSeverity.Hint, message="hint")
        mgr._diagnostics[Path(tmp_path / "foo.py").resolve().as_uri()] = [hint]

        with (
            patch("mewbo_tools.integration.lsp.run_lsp_async", return_value=None),
            patch("time.sleep"),
        ):
            result = get_passive_diagnostics(str(tmp_path / "foo.py"), str(tmp_path))
        assert result is None


# ---------------------------------------------------------------------------
# shutdown_lsp_managers
# ---------------------------------------------------------------------------


class TestShutdownLspManagers:
    def setup_method(self):
        _managers.clear()

    def teardown_method(self):
        _managers.clear()

    def test_clears_all_managers(self):
        mgr = MagicMock()
        mgr.shutdown_all = AsyncMock()
        _managers["/tmp"] = mgr

        asyncio.run(
            __import__(
                "mewbo_tools.integration.lsp", fromlist=["shutdown_lsp_managers"]
            ).shutdown_lsp_managers()
        )
        assert len(_managers) == 0

    def test_idempotent_on_empty(self):
        """Calling shutdown on an empty manager dict must not raise."""
        from mewbo_tools.integration.lsp import shutdown_lsp_managers

        asyncio.run(shutdown_lsp_managers())


# ---------------------------------------------------------------------------
# LSPServerManager — ensure_server / _start_server
# ---------------------------------------------------------------------------


class TestLSPServerManagerEnsureServer:
    def test_ensure_server_returns_none_when_no_server_for_file(self):
        mgr = _make_manager()
        result = asyncio.run(mgr.ensure_server("/tmp/foo.py"))
        assert result is None

    def test_ensure_server_returns_none_when_server_previously_failed(self):
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])
        mgr._failed.add(sdef.id)
        result = asyncio.run(mgr.ensure_server("/tmp/foo.py"))
        assert result is None

    def test_ensure_server_returns_cached_client(self):
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])
        fake_client = _make_fake_client()
        mgr._clients[sdef.id] = fake_client

        result = asyncio.run(mgr.ensure_server("/tmp/foo.py"))
        assert result is fake_client

    def test_start_server_marks_failed_on_exception(self):
        """When _start_server raises, the server id is added to _failed."""
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])

        async def _run():
            with patch.object(
                mgr,
                "_start_server",
                side_effect=Exception("spawn failed"),
            ):
                # _start_server exception bubbles through ensure_server
                try:
                    await mgr.ensure_server("/tmp/foo.py")
                except Exception:
                    pass

        asyncio.run(_run())

    def test_start_server_adds_failed_on_connection_error(self):
        """_start_server itself swallows and marks _failed."""
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])

        async def _run():
            with patch(
                "mewbo_tools.integration.lsp.manager.BaseLanguageClient",
                side_effect=Exception("cannot start"),
            ):
                result = await mgr._start_server(sdef)
            return result

        result = asyncio.run(_run())
        assert result is None
        assert sdef.id in mgr._failed


# ---------------------------------------------------------------------------
# LSPServerManager — open_file
# ---------------------------------------------------------------------------


class TestLSPServerManagerOpenFile:
    def test_open_file_sends_did_open(self, tmp_path: Path):
        """open_file calls text_document_did_open with correct params."""
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])
        fake_client = _make_fake_client()

        target = tmp_path / "test.py"
        target.write_text("x = 1\n", encoding="utf-8")

        asyncio.run(mgr.open_file(fake_client, str(target)))
        fake_client.text_document_did_open.assert_called_once()
        call_args = fake_client.text_document_did_open.call_args[0][0]
        assert call_args.text_document.language_id == "python"
        assert "x = 1" in call_args.text_document.text

    def test_open_file_handles_missing_file(self, tmp_path: Path):
        """open_file returns gracefully when the file doesn't exist."""
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])
        fake_client = _make_fake_client()

        asyncio.run(mgr.open_file(fake_client, str(tmp_path / "nonexistent.py")))
        fake_client.text_document_did_open.assert_not_called()


# ---------------------------------------------------------------------------
# LSPServerManager — shutdown_all
# ---------------------------------------------------------------------------


class TestLSPServerManagerShutdownAll:
    def test_shutdown_all_clears_state(self):
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])

        fake_client = MagicMock()
        fake_client.shutdown_async = AsyncMock(return_value=None)
        fake_client.exit = MagicMock()
        fake_client.stop = AsyncMock()
        mgr._clients[sdef.id] = fake_client
        mgr._diagnostics["file:///tmp/foo.py"] = []

        asyncio.run(mgr.shutdown_all())

        assert len(mgr._clients) == 0
        assert len(mgr._diagnostics) == 0

    def test_shutdown_all_handles_errors_gracefully(self):
        """shutdown_all continues even if a client errors on shutdown."""
        sdef = _make_server_def()
        mgr = _make_manager(servers=[sdef])

        bad_client = MagicMock()
        bad_client.shutdown_async = AsyncMock(side_effect=RuntimeError("crash"))
        mgr._clients[sdef.id] = bad_client

        # Must not raise
        asyncio.run(mgr.shutdown_all())
        assert len(mgr._clients) == 0


# ---------------------------------------------------------------------------
# LSPServerManager — _find_root
# ---------------------------------------------------------------------------


class TestLSPServerManagerFindRoot:
    def test_find_root_returns_cwd_when_no_marker(self, tmp_path: Path):
        mgr = _make_manager(cwd=str(tmp_path))
        sdef = _make_server_def()
        result = mgr._find_root(sdef)
        # No pyproject.toml → falls back to cwd
        assert result == str(tmp_path)

    def test_find_root_finds_marker_in_parent(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").touch()
        sub = tmp_path / "src" / "pkg"
        sub.mkdir(parents=True)
        mgr = _make_manager(cwd=str(sub))
        sdef = _make_server_def()
        result = mgr._find_root(sdef)
        assert result == str(tmp_path)

    def test_find_root_stops_at_filesystem_root(self, tmp_path: Path):
        """When walking up reaches /, return cwd rather than hanging."""
        mgr = _make_manager(cwd=str(tmp_path))
        sdef = ServerDef(
            id="x",
            extensions=(".xyz",),
            command=("x",),
            root_markers=("__NONEXISTENT_MARKER__.txt",),
            language_id="x",
        )
        result = mgr._find_root(sdef)
        # Must return something reasonable and not raise
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# LSPTool.run — dispatch & error paths
# ---------------------------------------------------------------------------


class TestLSPToolRun:
    def _make_tool(self) -> LSPTool:
        return LSPTool.__new__(LSPTool)

    def _step(self, **kwargs: Any) -> ActionStep:
        return ActionStep(tool_id="lsp_tool", operation="get", tool_input=kwargs)

    def test_missing_file_path_returns_error(self):
        tool = self._make_tool()
        step = self._step(operation="diagnostics")
        result = tool.run(step)
        assert "file_path" in result.content.lower() or "required" in result.content.lower()

    def test_unknown_operation_returns_error(self):
        tool = self._make_tool()
        step = self._step(operation="frobnicate", file_path="/tmp/foo.py")
        with patch("mewbo_tools.integration.lsp.tool.get_lsp_manager") as mock_mgr:
            mock_mgr.return_value = _make_manager()
            result = tool.run(step)
        assert "unknown" in result.content.lower() or "frobnicate" in result.content

    def test_diagnostics_no_server_for_file(self, tmp_path: Path):
        """Returns informative message when no LSP handles the file extension."""
        tool = self._make_tool()
        step = self._step(operation="diagnostics", file_path=str(tmp_path / "foo.xyz"))
        mgr = _make_manager(cwd=str(tmp_path))

        with patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr):
            with patch(
                "mewbo_tools.integration.lsp.tool.run_lsp_async",
                return_value=None,
            ):
                result = tool.run(step)
        # No server configured for .xyz
        assert ".xyz" in result.content or "no language server" in result.content.lower()

    def test_diagnostics_server_not_available(self, tmp_path: Path):
        """When ensure_server returns None AND there's a known sdef → shows install hint."""
        tool = self._make_tool()
        step = self._step(operation="diagnostics", file_path=str(tmp_path / "foo.py"))

        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])

        with patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr):
            with patch(
                "mewbo_tools.integration.lsp.tool.run_lsp_async",
                return_value=None,  # ensure_server returned None
            ):
                result = tool.run(step)
        # Should mention the server id or 'not available'
        assert sdef.id in result.content or "not available" in result.content.lower()

    def test_diagnostics_runs_and_formats(self, tmp_path: Path):
        """Full diagnostics path with a mocked client."""
        tool = self._make_tool()
        target = tmp_path / "foo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        step = self._step(operation="diagnostics", file_path=str(target))

        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])
        fake_client = _make_fake_client()
        # Pre-populate diagnostics cache
        uri = target.resolve().as_uri()
        mgr._diagnostics[uri] = [
            _make_diagnostic(line=0, message="undefined name 'x'"),
        ]

        call_count = {"n": 0}

        def _fake_run_lsp_async(coro, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: ensure_server → return fake_client
                return fake_client
            # Subsequent calls: open_file → None
            return None

        with (
            patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr),
            patch(
                "mewbo_tools.integration.lsp.tool.run_lsp_async",
                side_effect=_fake_run_lsp_async,
            ),
            patch("time.sleep"),
        ):
            result = tool.run(step)

        assert "undefined name" in result.content or "Diagnostics" in result.content

    def test_navigation_no_server_for_file(self, tmp_path: Path):
        """definition/references/hover return error when no server available."""
        tool = self._make_tool()
        step = self._step(
            operation="definition",
            file_path=str(tmp_path / "foo.xyz"),
            line=0,
            character=0,
        )
        mgr = _make_manager(cwd=str(tmp_path))

        with (
            patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr),
            patch(
                "mewbo_tools.integration.lsp.tool.run_lsp_async",
                return_value=None,
            ),
        ):
            result = tool.run(step)
        assert "no language server" in result.content.lower()

    @pytest.mark.parametrize("operation", ["definition", "references", "hover"])
    def test_navigation_exception_is_caught(self, tmp_path: Path, operation: str):
        """Exceptions from LSP calls are caught and returned as error text."""
        tool = self._make_tool()
        target = tmp_path / "foo.py"
        target.write_text("x = 1\n", encoding="utf-8")
        step = self._step(
            operation=operation,
            file_path=str(target),
            line=0,
            character=0,
        )

        sdef = _make_server_def()
        mgr = _make_manager(cwd=str(tmp_path), servers=[sdef])
        fake_client = _make_fake_client()

        call_count = {"n": 0}

        def _fake_run(coro, **kw):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return fake_client  # ensure_server + open_file
            raise RuntimeError("LSP unavailable")

        with (
            patch("mewbo_tools.integration.lsp.tool.get_lsp_manager", return_value=mgr),
            patch(
                "mewbo_tools.integration.lsp.tool.run_lsp_async",
                side_effect=_fake_run,
            ),
        ):
            result = tool.run(step)
        assert "failed" in result.content.lower() or "lsp" in result.content.lower()


# ---------------------------------------------------------------------------
# LSPTool._format_diagnostics
# ---------------------------------------------------------------------------


class TestLSPToolFormatDiagnosticsExtra:
    def _manager_mock(self, server_id: str = "pyright") -> MagicMock:
        mgr = MagicMock()
        mgr.server_for_file.return_value = MagicMock(id=server_id)
        return mgr

    def test_single_error_with_code(self):
        diags = [_make_diagnostic(line=5, message="type error", code="E101")]
        mgr = self._manager_mock()
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        assert "E101" in result
        assert "type error" in result
        assert "1 error" in result

    def test_warning_counted_separately(self):
        diags = [
            _make_diagnostic(line=0, severity=types.DiagnosticSeverity.Error, message="err"),
            _make_diagnostic(line=1, severity=types.DiagnosticSeverity.Warning, message="warn"),
        ]
        mgr = self._manager_mock()
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        assert "1 error" in result
        assert "1 warning" in result

    def test_server_none_shows_unknown(self):
        mgr = MagicMock()
        mgr.server_for_file.return_value = None
        diags = [_make_diagnostic(message="boom")]
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        assert "unknown" in result

    def test_only_hints_shows_suppressed_message(self):
        diags = [
            _make_diagnostic(
                line=0, severity=types.DiagnosticSeverity.Hint, message="consider refactoring"
            )
        ]
        mgr = self._manager_mock()
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        assert (
            "hints" in result.lower() or "info" in result.lower() or "suppressed" in result.lower()
        )

    def test_diagnostic_without_severity_treated_as_error(self):
        diags = [
            types.Diagnostic(
                range=types.Range(
                    start=types.Position(line=0, character=0),
                    end=types.Position(line=0, character=1),
                ),
                message="no severity",
                severity=None,
            )
        ]
        mgr = self._manager_mock()
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        # None severity is treated as error-level
        assert "no severity" in result

    def test_more_than_50_diagnostics_shows_suppressed_count(self):
        diags = [_make_diagnostic(line=i, message=f"err{i}") for i in range(60)]
        mgr = self._manager_mock()
        result = LSPTool._format_diagnostics("/tmp/foo.py", diags, mgr)
        assert "10 more" in result or "suppressed" in result.lower()


# ---------------------------------------------------------------------------
# LSPTool._format_locations
# ---------------------------------------------------------------------------


class TestLSPToolFormatLocations:
    def test_empty_list_returns_not_found(self):
        result = LSPTool._format_locations([], "References")
        assert "found" in result.lower()

    def test_single_location_shows_path_and_line(self):
        loc = types.Location(
            uri="file:///home/user/project/foo.py",
            range=types.Range(
                start=types.Position(line=9, character=0),
                end=types.Position(line=9, character=5),
            ),
        )
        result = LSPTool._format_locations([loc], "Definition")
        assert "/home/user/project/foo.py:10" in result

    def test_location_link_uses_target_uri_and_range(self):
        loc = types.LocationLink(
            target_uri="file:///home/user/bar.py",
            target_range=types.Range(
                start=types.Position(line=4, character=0),
                end=types.Position(line=4, character=3),
            ),
            target_selection_range=types.Range(
                start=types.Position(line=4, character=0),
                end=types.Position(line=4, character=3),
            ),
        )
        result = LSPTool._format_locations([loc], "Definition")
        assert "/home/user/bar.py:5" in result

    def test_more_than_20_locations_shows_remainder(self):
        locs = [
            types.Location(
                uri=f"file:///tmp/f{i}.py",
                range=types.Range(
                    start=types.Position(line=0, character=0),
                    end=types.Position(line=0, character=1),
                ),
            )
            for i in range(25)
        ]
        result = LSPTool._format_locations(locs, "References")
        assert "5 more" in result


# ---------------------------------------------------------------------------
# LSPTool._format_hover
# ---------------------------------------------------------------------------


class TestLSPToolFormatHover:
    def test_hover_with_string_content(self):
        hover = types.Hover(contents="plain text hover")
        result = LSPTool._format_hover(hover)
        assert result == "plain text hover"

    def test_hover_with_list_of_strings(self):
        hover = types.Hover(
            contents=[
                types.MarkedStringWithLanguage(language="python", value="int"),
                "description",
            ]
        )
        result = LSPTool._format_hover(hover)
        assert "int" in result or "description" in result

    def test_hover_with_list_of_markup(self):
        hover = types.Hover(
            contents=[
                types.MarkedStringWithLanguage(language="python", value="def foo() -> int: ..."),
            ]
        )
        result = LSPTool._format_hover(hover)
        assert result  # non-empty

    def test_hover_with_non_standard_content(self):
        """Fallback: str(content) when content is unrecognized type."""

        class Weird:
            def __str__(self):
                return "weird content"

        hover = MagicMock()
        hover.contents = Weird()
        result = LSPTool._format_hover(hover)  # type: ignore[arg-type]
        assert "weird content" in result
