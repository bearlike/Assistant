#!/usr/bin/env python3
"""First-party plugin implementation of the ``submit_widget`` SessionTool.

This module replaces the pre-refactor core module ``widget_builder.py``.
Nothing about widgets leaks into core now; the core only sees a generic
:class:`SessionTool` via the plugin ``session_tools`` manifest entry — the
widget-specific Pydantic schema, path-traversal guard, event shape, and
termination flag all live here inside the built-in plugin.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from meeseeks_core.builtin_plugins.widget_builder.linter import (
    ALLOWED_MODULES,
    format_findings,
    lint,
)
from meeseeks_core.common import (
    MockSpeaker,
    get_logger,
    pydantic_to_openai_tool,
)
from meeseeks_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from meeseeks_core.classes import ActionStep
    from meeseeks_core.types import Event

logging = get_logger(name="core.builtin_plugins.widget_builder")


# ------------------------------------------------------------------
# Widget root resolution (env-driven; no config coupling)
# ------------------------------------------------------------------

_DEFAULT_WIDGET_ROOT = "/tmp/meeseeks/widgets"


def _widget_root() -> str:
    """Return the base widget directory.

    Reads ``MEESEEKS_WIDGET_ROOT`` from the environment and falls back to
    ``/tmp/meeseeks/widgets``. The agent prompt uses the same
    ``${MEESEEKS_WIDGET_ROOT:-/tmp/meeseeks/widgets}`` pattern so writer
    and reader agree on the target directory without any config plumbing.

    An empty-string env var is treated as unset to avoid ``Path("")`` silently
    resolving to the process CWD.
    """
    return os.environ.get("MEESEEKS_WIDGET_ROOT") or _DEFAULT_WIDGET_ROOT


# ------------------------------------------------------------------
# Tool args — Pydantic is the single source of truth for schema + validation
# ------------------------------------------------------------------


class SubmitWidgetArgs(BaseModel):
    """Signal that the Streamlit widget is complete and ready for rendering.

    Call this ONLY after writing ``app.py`` and ``data.json`` to the widget
    directory. The tool reads the files from disk — do not pass contents in
    arguments. Use only if you were spawned as a st-widget-builder agent.
    """

    model_config = ConfigDict(extra="forbid")

    widget_id: str = Field(
        description="Unique widget identifier matching the directory name (no path separators).",
    )
    requirements: list[str] = Field(
        default_factory=list,
        description="Pure-Python packages to install in stlite (e.g. ['pandas', 'plotly']).",
    )
    summary: str = Field(
        default="",
        description="One-line description of what this widget shows.",
    )

    @field_validator("widget_id")
    @classmethod
    def _no_path_traversal(cls, v: str) -> str:
        if not v or "/" in v or "\\" in v or ".." in v or v.startswith("."):
            raise ValueError(
                "widget_id must be a plain identifier (no '/', '\\', '..', or leading '.')"
            )
        return v


# Derived from SubmitWidgetArgs — same shape the core SPAWN_AGENT_SCHEMA uses.
SUBMIT_WIDGET_SCHEMA: dict[str, object] = pydantic_to_openai_tool(
    SubmitWidgetArgs, name="submit_widget"
)


# ------------------------------------------------------------------
# SessionTool implementation
# ------------------------------------------------------------------


class SubmitWidgetTool:
    """Handles ``submit_widget`` tool calls in st-widget-builder sub-agents.

    Validates args via Pydantic, reads the generated widget files from
    ``${MEESEEKS_WIDGET_ROOT}/{session_id}/{widget_id}/`` (default
    ``/tmp/meeseeks/widgets``), emits a ``widget_ready`` event, and sets a
    termination flag so the sub-agent loop exits cleanly.
    """

    # Class-level attributes satisfy the ``SessionTool`` Protocol natively —
    # no extra adapter layer required by ``SessionToolRegistry``.
    tool_id: str = "submit_widget"
    schema: dict[str, object] = SUBMIT_WIDGET_SCHEMA
    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(
        self,
        *,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialize the handler.

        Args:
            session_id: Session identifier used to resolve the widget path.
            event_logger: Callback for emitting ``widget_ready`` events; usually
                ``agent_context.event_logger``.
        """
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate_run_pending = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        if self._terminate_run_pending:
            self._terminate_run_pending = False
            return True
        return False

    def _emit(self, event: Event) -> None:
        if self._event_logger is None:
            return
        try:
            self._event_logger(event)
        except Exception as exc:
            logging.warning("submit_widget event emit failed: {}", exc)

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``submit_widget`` tool call."""
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = SubmitWidgetArgs.model_validate(raw)
        except ValidationError as exc:
            return MockSpeaker(content=f"ERROR: invalid submit_widget args: {exc}")

        base = _widget_root()
        widget_dir = (Path(base) / self._session_id / args.widget_id).resolve()
        root = Path(base).resolve()
        try:
            widget_dir.relative_to(root)
        except ValueError:
            return MockSpeaker(
                content=f"ERROR: widget_id '{args.widget_id}' escapes the widget root."
            )
        app_path = widget_dir / "app.py"
        data_path = widget_dir / "data.json"

        if not app_path.exists():
            return MockSpeaker(
                content=f"ERROR: app.py not found at {app_path}. Write the file first."
            )
        if not data_path.exists():
            return MockSpeaker(
                content=f"ERROR: data.json not found at {data_path}. Write the file first."
            )

        try:
            py_code = app_path.read_text(encoding="utf-8")
            json_data = data_path.read_text(encoding="utf-8")
        except OSError as exc:
            return MockSpeaker(content=f"ERROR: Failed to read widget files: {exc}")

        # Authoritative lint gate — agent-side ruff / ast.parse can't see the
        # stlite/pyodide allowlist, so the tool itself must enforce it before
        # a broken widget reaches the console.  Error names every offender +
        # the full allowlist so the agent can self-correct on the next turn.
        findings = lint(py_code)
        if findings:
            return MockSpeaker(
                content=(
                    f"ERROR: widget lint failed for {app_path}:\n"
                    f"{format_findings(findings)}\n\n"
                    f"Allowed modules: {sorted(ALLOWED_MODULES)}\n"
                    f"Fix these issues and resubmit."
                )
            )

        # The core ``Event`` union dropped the widget-specific payload type when
        # the feature moved out of core. The generic ``dict`` branch of the
        # payload union absorbs the emit — no narrower type would buy us
        # anything without re-coupling core to the widget shape.
        payload: dict[str, object] = {
            "widget_id": args.widget_id,
            "session_id": self._session_id,
            "files": {"app.py": py_code, "data.json": json_data},
            "requirements": list(args.requirements),
            "summary": args.summary,
        }
        self._emit({"type": "widget_ready", "payload": payload})
        self._terminate_run_pending = True
        return MockSpeaker(content=f"Widget '{args.widget_id}' submitted successfully.")


__all__ = [
    "SUBMIT_WIDGET_SCHEMA",
    "SubmitWidgetArgs",
    "SubmitWidgetTool",
]
