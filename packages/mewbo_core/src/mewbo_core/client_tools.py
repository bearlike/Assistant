#!/usr/bin/env python3
"""Client-declared, client-fulfilled session tools (Gitea #179, Phase 1).

A client (e.g. the Aura Android app) puts raw JSON-Schema tool declarations on
``context.device_tools`` when it starts a run. The api validates each entry
into a :class:`ClientToolSpec` and binds a :class:`ClientDeclaredTool` — a
plain :class:`~mewbo_core.session_tools.SessionTool` — into that run's
toolset. When the agent calls one, the tool delegates to whatever concrete
dispatcher the app registered through the :class:`DeviceToolDispatcher` seam
(mirrors ``mewbo_graph.scg.search_launcher.SearchLauncher`` — a down-only DI
push, so this module never imports up into an app). Adding a new device tool
is therefore a CLIENT-only change: the server carries zero per-tool code,
only this one generic bridge.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mewbo_core.classes import ActionStep
from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

logging = get_logger(name="core.client_tools")

# ``device_`` prefix so a client-declared id can never shadow a built-in or
# plugin tool id — ``ToolUseLoop._execute_tool_call`` matches SessionTools by
# ``tool_id`` BEFORE the stateless ToolRegistry, so an unconstrained id could
# otherwise impersonate an internal tool.
_TOOL_ID_RE = re.compile(r"^device_[a-z0-9_]{1,48}$")


class ClientToolSpec(BaseModel):
    """One client-declared tool schema, validated at definition."""

    model_config = ConfigDict(extra="forbid")

    tool_id: str = Field(description="Must match ^device_[a-z0-9_]{1,48}$.")
    description: str = Field(description="Non-empty tool description shown to the model.")
    parameters: dict[str, Any] = Field(
        description="JSON-Schema object describing the tool's arguments."
    )

    @field_validator("tool_id")
    @classmethod
    def _validate_tool_id(cls, value: str) -> str:
        if not _TOOL_ID_RE.match(value):
            raise ValueError(
                f"tool_id must match ^device_[a-z0-9_]{{1,48}}$ (got {value!r})"
            )
        return value

    @field_validator("description")
    @classmethod
    def _validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must be non-empty")
        return value

    @field_validator("parameters")
    @classmethod
    def _validate_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        declared_type = value.get("type")
        if declared_type is not None and declared_type != "object":
            raise ValueError(
                f"parameters.type must be 'object' when present (got {declared_type!r})"
            )
        return value


class DeviceToolDispatcherImpl(Protocol):
    """The concrete dispatcher an app registers (session-transport bound)."""

    async def dispatch(
        self, session_id: str, tool_id: str, tool_input: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute one device-tool call and return its final result payload.

        Returns ``{"status": "ok", "result": ...}`` or ``{"status": "error",
        "error": {"code": ..., "message": ...}}``.
        """
        ...


class DeviceToolDispatcher:
    """Process-wide injectable dispatcher for client-declared device tools.

    Down-only DI push seam mirroring ``mewbo_graph.scg.search_launcher.
    SearchLauncher``: an app (the api) registers a concrete implementation at
    startup; this module never imports up to find it. No dispatcher
    registered — a core/graph-only install, or the api never initialised —
    degrades every call to ``None``, and :class:`ClientDeclaredTool` turns
    that into a structured "unavailable" error rather than crashing.
    """

    _impl: ClassVar[DeviceToolDispatcherImpl | None] = None

    @classmethod
    def register(cls, impl: DeviceToolDispatcherImpl | None) -> None:
        """Install the concrete dispatcher (called by the api at startup)."""
        cls._impl = impl

    @classmethod
    def reset(cls) -> None:
        """Clear the registered dispatcher (test isolation)."""
        cls._impl = None

    @classmethod
    def available(cls) -> bool:
        """True when a concrete dispatcher is registered."""
        return cls._impl is not None

    @classmethod
    async def dispatch(
        cls, session_id: str, tool_id: str, tool_input: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Dispatch via the registered impl; ``None`` when none is wired."""
        if cls._impl is None:
            return None
        return await cls._impl.dispatch(session_id, tool_id, tool_input)


_DEFAULT_ERROR_CODE = "device_error"
_DEFAULT_ERROR_MESSAGE = "Device reported an unspecified error."


def _error_envelope(code: str, message: str) -> MockSpeaker:
    """Structured error envelope matching the graph plugins' ``_err_result`` shape.

    ``tool_use_loop._session_tool_error_envelope`` recognises exactly this
    shape (the ``str({"error": {...}})`` repr) and reclassifies the step as a
    FAILED tool result while still handing the model the envelope text.
    Shared by BOTH our own dispatch-unavailability AND a dispatcher's own
    reported error status — a device tool must fail the same way an
    unavailable/erroring graph tool does; a raw ``{"status":"error",...}``
    JSON dump is invisible to the detector (it only matches an ``'error'``
    top-level key), so an unenveloped error would round-trip as
    ``success=True`` — losing the per-step failure nudge and doom-loop
    tracking even though the model saw the error text.

    A blank ``code`` AND blank ``message`` together is ALSO invisible to the
    detector — it treats ``{"code": "", "message": ""}`` as "not an error
    envelope" (both empty ⇒ ``None``) and the step would STILL record
    ``success=True`` despite being a genuine dispatcher-reported failure
    (reachable: the result route accepts ``{"status":"error","error":{}}``).
    Default each blank field independently so a blank pair can never reach
    the detector unenveloped.
    """
    resolved_code = code.strip() or _DEFAULT_ERROR_CODE
    resolved_message = message.strip() or _DEFAULT_ERROR_MESSAGE
    return MockSpeaker(
        content=str({"error": {"code": resolved_code, "message": resolved_message}})
    )


def _unavailable_result(tool_id: str) -> MockSpeaker:
    """Envelope for "no dispatcher registered" — our own failure to dispatch."""
    return _error_envelope(
        "device_tool_unavailable",
        f"No device-tool dispatcher registered for '{tool_id}'.",
    )


class ClientDeclaredTool:
    """A ``SessionTool`` wrapping one client-declared device tool.

    Constructed per ``(session_id, spec)`` pair by the api when it builds a
    run's ``extra_session_tools``. The OpenAI function schema is built
    VERBATIM from the spec — no server-side mutation or field injection — so
    the model sees exactly what the client declared.
    """

    modes: frozenset[str] = DEFAULT_SESSION_TOOL_MODES

    def __init__(self, session_id: str, spec: ClientToolSpec) -> None:
        """Bind the session id and validated spec; build the verbatim schema."""
        self._session_id = session_id
        self._spec = spec
        self.tool_id: str = spec.tool_id
        self.schema: dict[str, object] = {
            "type": "function",
            "function": {
                "name": spec.tool_id,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }

    def should_terminate_run(self) -> bool:
        """Never terminates the run — a device-tool call is an ordinary tool result."""
        return False

    def terminal_reason(self) -> str:
        """Unused (never terminates); parity with the Protocol default."""
        return "awaiting_approval"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Dispatch the call via the registered ``DeviceToolDispatcher``.

        No dispatcher registered → a structured unavailable error. A
        registered dispatcher's ``status: "error"`` result (device timeout,
        permission denial, etc.) is reshaped into the SAME error envelope —
        the loop's ``_session_tool_error_envelope`` only recognises the
        ``{'error': {...}}`` shape, so an unenveloped ``{"status":"error",...}``
        would silently record as a successful step. A ``status: "ok"`` result
        rides through verbatim as JSON, unmodified.
        """
        tool_input = (
            action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        )
        result = await DeviceToolDispatcher.dispatch(self._session_id, self.tool_id, tool_input)
        if result is None:
            return _unavailable_result(self.tool_id)
        if result.get("status") == "error":
            error = result.get("error")
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            return _error_envelope(code, message)
        return MockSpeaker(content=json.dumps(result))


__all__ = [
    "ClientDeclaredTool",
    "ClientToolSpec",
    "DeviceToolDispatcher",
    "DeviceToolDispatcherImpl",
]
