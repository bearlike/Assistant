#!/usr/bin/env python3
"""Tests for CLI KeyListener (keystroke capture during Rich Live)."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from meeseeks_cli.cli_keys import KeyListener


class TestKeyListenerNoop:
    """KeyListener should be a safe no-op when stdin is not a TTY."""

    def test_enter_exit_noop_when_not_tty(self):
        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            listener = KeyListener()
            listener.bind("x", lambda: None)
            with listener:
                assert listener._fd is None
                assert listener._thread is None

    def test_pause_resume_noop_when_not_tty(self):
        listener = KeyListener()
        # No __enter__ called, _fd is None.
        listener.pause()   # should not raise
        listener.resume()  # should not raise


class TestKeyListenerBindings:
    """Test bind/dispatch without a real terminal."""

    @patch("meeseeks_cli.cli_keys.tty")
    @patch("meeseeks_cli.cli_keys.termios")
    @patch("meeseeks_cli.cli_keys.select")
    @patch("meeseeks_cli.cli_keys.os")
    @patch("sys.stdin")
    def test_bound_key_fires_callback(
        self, mock_stdin, mock_os, mock_select, mock_termios, mock_tty,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        mock_termios.tcgetattr.return_value = []

        callback = MagicMock()
        fired = threading.Event()

        def _callback() -> None:
            callback()
            fired.set()

        # First select returns readable, then we stop.
        call_count = 0

        def _select_side_effect(rlist, _w, _e, _t):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (rlist, [], [])
            return ([], [], [])

        mock_select.select.side_effect = _select_side_effect
        mock_os.read.return_value = b"x"

        listener = KeyListener()
        listener.bind("x", _callback)

        with listener:
            fired.wait(timeout=2.0)

        assert callback.called, "Callback should have been invoked"

    @patch("meeseeks_cli.cli_keys.tty")
    @patch("meeseeks_cli.cli_keys.termios")
    @patch("meeseeks_cli.cli_keys.select")
    @patch("meeseeks_cli.cli_keys.os")
    @patch("sys.stdin")
    def test_unbound_key_ignored(
        self, mock_stdin, mock_os, mock_select, mock_termios, mock_tty,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        mock_termios.tcgetattr.return_value = []

        callback = MagicMock()

        call_count = 0

        def _select_side_effect(rlist, _w, _e, _t):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (rlist, [], [])
            return ([], [], [])

        mock_select.select.side_effect = _select_side_effect
        mock_os.read.return_value = b"z"  # unbound key

        listener = KeyListener()
        listener.bind("x", callback)  # only "x" is bound

        with listener:
            # Give reader thread a moment to process.
            threading.Event().wait(0.3)

        assert not callback.called, "Callback should not fire for unbound key"


class TestKeyListenerLifecycle:
    """Test start/stop and pause/resume mechanics."""

    @patch("meeseeks_cli.cli_keys.tty")
    @patch("meeseeks_cli.cli_keys.termios")
    @patch("meeseeks_cli.cli_keys.select")
    @patch("meeseeks_cli.cli_keys.os")
    @patch("sys.stdin")
    def test_stop_sets_event_and_joins(
        self, mock_stdin, mock_os, mock_select, mock_termios, mock_tty,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        mock_termios.tcgetattr.return_value = []
        mock_select.select.return_value = ([], [], [])

        listener = KeyListener()
        with listener:
            assert listener._thread is not None
            assert listener._thread.is_alive()

        # After exit, stop event should be set.
        assert listener._stop.is_set()
        # Terminal settings should be restored.
        mock_termios.tcsetattr.assert_called()

    @patch("meeseeks_cli.cli_keys.tty")
    @patch("meeseeks_cli.cli_keys.termios")
    @patch("meeseeks_cli.cli_keys.select")
    @patch("meeseeks_cli.cli_keys.os")
    @patch("sys.stdin")
    def test_pause_sets_event_and_restores_termios(
        self, mock_stdin, mock_os, mock_select, mock_termios, mock_tty,
    ):
        mock_stdin.isatty.return_value = True
        mock_stdin.fileno.return_value = 0
        old_settings = [1, 2, 3]
        mock_termios.tcgetattr.return_value = old_settings
        mock_select.select.return_value = ([], [], [])

        listener = KeyListener()
        with listener:
            listener.pause()
            assert listener._paused.is_set()
            # Should have restored original settings.
            mock_termios.tcsetattr.assert_called_with(
                0, mock_termios.TCSADRAIN, old_settings,
            )

            listener.resume()
            assert not listener._paused.is_set()
            assert listener._resumed.is_set()
            mock_tty.setcbreak.assert_called()
