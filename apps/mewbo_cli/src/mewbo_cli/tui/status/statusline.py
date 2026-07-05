#!/usr/bin/env python3
"""StatusLineRunner — user statusLine script over a JSON-on-stdin contract (#156).

Mirrors the prompt-registry / MCP-config *file-contract* philosophy: the user
points a config knob at an executable, we hand it a **stable JSON document on
stdin** describing the current session state, and render whatever it prints to
stdout as the status line. The schema is the contract — additive only.

Stable JSON schema (top-level keys; never remove or repurpose one):

    {
      "model": "openai/gpt-oss-120b",
      "provider": "openai",
      "cwd": "/home/me/project",
      "git": {"branch": "main", "worktree": null},
      "context_window": {"used": 1234, "total": 128000, "percent": 0.96,
                          "estimated": false},
      "cost": 0.0123,            // null when unknown
      "tokens": {"input": 900, "output": 334, "total": 1234},
      "session_id": "…"
    }

The runner is **graceful by construction**: no configured script → disabled
(``run`` returns ``None``); a script that errors, times out, or is missing →
``None`` (the status bar just shows its built-in line). Nothing here raises.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from mewbo_core.config import get_config_value

#: Config knobs (read via ``get_config_value``). Documented in CLAUDE.md.
SCRIPT_CONFIG_KEYS = ("cli", "statusline", "script")
INTERVAL_CONFIG_KEYS = ("cli", "statusline", "interval_seconds")

#: Default refresh cadence when the knob is unset, and the hard floor — a script
#: re-run every render would be wasteful, so we clamp.
DEFAULT_INTERVAL = 5.0
MIN_INTERVAL = 1.0

#: Hard wall-clock cap on one script invocation so a hung script can never wedge
#: the refresh timer.
_RUN_TIMEOUT = 5.0


@dataclass(frozen=True)
class StatusLineState:
    """The session snapshot serialized to the script's stdin.

    A plain value object so the caller (the installer's refresh tick) can build
    it from whatever it has on hand; :meth:`StatusLineRunner.build_payload`
    turns it into the wire schema above.
    """

    model: str | None = None
    provider: str | None = None
    cwd: str | None = None
    git_branch: str | None = None
    git_worktree: str | None = None
    context_used: int = 0
    context_total: int = 0
    context_percent: float = 0.0
    context_estimated: bool = False
    cost: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    session_id: str | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


class StatusLineRunner:
    """Run the configured statusLine script with session JSON on stdin.

    One atomic class: it resolves the script path + interval from config (or
    explicit constructor overrides for tests), serializes a
    :class:`StatusLineState` to the stable schema, executes the script, and
    returns its trimmed stdout. Every failure mode degrades to ``None``.
    """

    def __init__(
        self,
        *,
        script: str | None = None,
        interval: float | None = None,
        timeout: float = _RUN_TIMEOUT,
    ) -> None:
        """Bind explicit overrides; unset values fall back to config at use time."""
        self._script = script
        self._interval = interval
        self._timeout = timeout

    # -- config resolution ------------------------------------------------

    @property
    def script(self) -> str | None:
        """The configured script command, or ``None`` when unset/disabled."""
        if self._script is not None:
            return self._script or None
        value = get_config_value(*SCRIPT_CONFIG_KEYS, default=None)
        return str(value) if value else None

    @property
    def enabled(self) -> bool:
        """Whether a status script is configured."""
        return bool(self.script)

    @property
    def interval(self) -> float:
        """Refresh cadence in seconds, clamped to :data:`MIN_INTERVAL`."""
        raw = self._interval
        if raw is None:
            raw = get_config_value(*INTERVAL_CONFIG_KEYS, default=DEFAULT_INTERVAL)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            value = DEFAULT_INTERVAL
        return max(MIN_INTERVAL, value)

    # -- payload ----------------------------------------------------------

    @staticmethod
    def build_payload(state: StatusLineState) -> dict[str, Any]:
        """Serialize ``state`` into the stable JSON schema (a plain dict)."""
        total = max(0, state.input_tokens) + max(0, state.output_tokens)
        payload: dict[str, Any] = {
            "model": state.model,
            "provider": state.provider,
            "cwd": state.cwd,
            "git": {"branch": state.git_branch, "worktree": state.git_worktree},
            "context_window": {
                "used": state.context_used,
                "total": state.context_total,
                "percent": round(state.context_percent, 4),
                "estimated": state.context_estimated,
            },
            "cost": state.cost,
            "tokens": {
                "input": state.input_tokens,
                "output": state.output_tokens,
                "total": total,
            },
            "session_id": state.session_id,
        }
        if state.extra:
            payload.update(dict(state.extra))
        return payload

    # -- execution --------------------------------------------------------

    def run(self, state: StatusLineState) -> str | None:
        """Run the script with the ``state`` JSON on stdin; return its stdout.

        Returns ``None`` when no script is configured or on any failure (missing
        executable, non-zero exit, timeout, decode error). The returned string
        is stripped of a single trailing newline; multi-line output is allowed
        (the status bar collapses it to its first line when it must fit one row).
        """
        script = self.script
        if not script:
            return None
        try:
            argv = shlex.split(script)
        except ValueError:
            return None
        if not argv:
            return None
        payload = json.dumps(self.build_payload(state))
        try:
            completed = subprocess.run(  # noqa: S603 - user-configured command, intentional
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        out = completed.stdout
        if out.endswith("\n"):
            out = out[:-1]
        return out or None


__all__ = [
    "DEFAULT_INTERVAL",
    "INTERVAL_CONFIG_KEYS",
    "MIN_INTERVAL",
    "SCRIPT_CONFIG_KEYS",
    "StatusLineRunner",
    "StatusLineState",
]
