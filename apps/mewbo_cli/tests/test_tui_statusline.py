#!/usr/bin/env python3
"""Tests for StatusLineRunner — JSON-on-stdin script contract (issue #156)."""

from __future__ import annotations

import json
import sys

from mewbo_cli.tui.status.statusline import (
    DEFAULT_INTERVAL,
    MIN_INTERVAL,
    StatusLineRunner,
    StatusLineState,
)


def test_disabled_when_no_script() -> None:
    """No configured script → disabled, run() returns None."""
    runner = StatusLineRunner(script="")
    assert runner.enabled is False
    assert runner.run(StatusLineState()) is None


def test_build_payload_has_stable_schema() -> None:
    """The payload carries the documented top-level keys."""
    state = StatusLineState(
        model="openai/gpt-oss-120b",
        provider="openai",
        cwd="/tmp/x",
        git_branch="main",
        context_used=1234,
        context_total=128000,
        context_percent=0.964,
        context_estimated=False,
        cost=0.0123,
        input_tokens=900,
        output_tokens=334,
        session_id="sess-1",
    )
    payload = StatusLineRunner.build_payload(state)
    assert payload["model"] == "openai/gpt-oss-120b"
    assert payload["provider"] == "openai"
    assert payload["cwd"] == "/tmp/x"
    assert payload["git"] == {"branch": "main", "worktree": None}
    assert payload["context_window"] == {
        "used": 1234,
        "total": 128000,
        "percent": 0.964,
        "estimated": False,
    }
    assert payload["cost"] == 0.0123
    assert payload["tokens"] == {"input": 900, "output": 334, "total": 1234}
    assert payload["session_id"] == "sess-1"


def test_run_passes_json_on_stdin_and_returns_stdout() -> None:
    """A script reading stdin JSON and printing a field echoes it back."""
    script = (
        f"{sys.executable} -c "
        '"import sys,json;d=json.load(sys.stdin);print(d[\'model\'])"'
    )
    runner = StatusLineRunner(script=script)
    out = runner.run(StatusLineState(model="my-model"))
    assert out == "my-model"


def test_run_strips_single_trailing_newline() -> None:
    """A single trailing newline from the script is stripped."""
    script = f'{sys.executable} -c "print(\'hello\')"'
    runner = StatusLineRunner(script=script)
    assert runner.run(StatusLineState()) == "hello"


def test_run_nonzero_exit_returns_none() -> None:
    """A non-zero exit degrades to None."""
    script = f'{sys.executable} -c "import sys;sys.exit(3)"'
    runner = StatusLineRunner(script=script)
    assert runner.run(StatusLineState()) is None


def test_run_missing_executable_returns_none() -> None:
    """A missing executable degrades to None (no raise)."""
    runner = StatusLineRunner(script="/no/such/binary/abc123")
    assert runner.run(StatusLineState()) is None


def test_run_timeout_returns_none() -> None:
    """A script exceeding the timeout returns None rather than wedging."""
    script = f'{sys.executable} -c "import time;time.sleep(5)"'
    runner = StatusLineRunner(script=script, timeout=0.2)
    assert runner.run(StatusLineState()) is None


def test_interval_defaults_and_clamps() -> None:
    """Interval falls back to the default and is clamped to the floor."""
    assert StatusLineRunner(interval=None, script="x").interval == DEFAULT_INTERVAL
    assert StatusLineRunner(interval=0.1, script="x").interval == MIN_INTERVAL
    assert StatusLineRunner(interval=10.0, script="x").interval == 10.0


def test_payload_is_valid_json() -> None:
    """The payload round-trips through json.dumps/loads without error."""
    payload = StatusLineRunner.build_payload(StatusLineState(model="m"))
    assert json.loads(json.dumps(payload))["model"] == "m"
