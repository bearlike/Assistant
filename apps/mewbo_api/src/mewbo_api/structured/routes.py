"""Structured-response REST routes — ``/v1/structured``.

Schema-constrained, tool-using, workspace-grounded synthesis over the core
:class:`~mewbo_core.structured_response.StructuredResponder`. Mirrors the
``agentic_search`` mount pattern: a module-global auth guard + runtime injected
by :func:`init_structured`; the namespace is mounted at ``/v1/structured``.

The ``POST`` kicks the run off via the core async handle
(:meth:`StructuredResponder.start_async` → a ``"<session_id>:r<seq>"`` run_id)
and does a SHORT bounded await for fast completion; a ``GET /<run_id>`` resolves
that handle back to its session and reads the latest ``structured_output``
transcript event. Failures ALWAYS return a structured ``{error: {code, reason}}``
envelope — never an empty ``failed:`` string, a raw exception, or the internal
``emit_result`` tool name.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from flask import request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.structured_response import (
    STRUCTURED_OUTPUT_EVENT,
    StructuredResponder,
    StructuredResponseError,
)

logging = get_logger(name="api.structured.routes")

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]

# Bounded fast-path await: how long the POST waits for a quick completion
# before returning ``running``. Kept short so the request never blocks a worker
# for long — slow runs are polled via ``GET /v1/structured/<run_id>``.
_FAST_AWAIT_SECONDS = 4.0
_FAST_AWAIT_INTERVAL = 0.1

# Marker the emit tool writes into a ``structured_output`` payload when schema
# validation never succeeded (see ``EmitStructuredResponseTool``). Treated as an
# error, never surfaced as ``output``.
_VALIDATION_ERROR_KEY = "_error"
_NO_RESULT_REASON = "model did not emit a structured result; retry with a simpler schema"

# Terminal session statuses (from ``summarize_session``) — a terminal session
# with no ``structured_output`` is a hard "no result" error, not "still running".
_TERMINAL_STATUSES = frozenset({"completed", "incomplete", "failed", "canceled"})


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth
_runtime: Any = None

structured_ns = Namespace(
    "structured",
    description="Schema-constrained, tool-using structured responses.",
)


def init_structured(api: object, require_api_key: AuthGuard, runtime: Any = None) -> None:
    """Wire the namespace + capture the auth guard and the session runtime."""
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(structured_ns, path="/v1/structured")  # type: ignore[attr-defined]


def _error(code: int, reason: str) -> tuple[dict, int]:
    """Build the canonical structured error envelope + matching HTTP status."""
    return {"error": {"code": code, "reason": reason}}, code


def _session_id_of(run_id: str) -> str:
    """Recover the backing session id from a ``"<session_id>:r<seq>"`` run_id.

    Split on the FIRST ``:`` so a session id that itself contains a colon is
    preserved (the run seq token is always the trailing segment).
    """
    return run_id.split(":", 1)[0]


def _load_structured_output(session_id: str) -> object | None:
    """Return the LATEST ``structured_output`` payload for *session_id*, or None.

    Reads the transcript through the runtime's session store (the single read
    seam). Returns ``None`` when no such event has been emitted yet.
    """
    if _runtime is None:
        return None
    events = _runtime.session_store.load_transcript(session_id)
    payload: object | None = None
    for event in events:
        if event.get("type") == STRUCTURED_OUTPUT_EVENT:
            payload = event.get("payload")
    return payload


def _is_validation_error_payload(payload: object) -> bool:
    """True when a ``structured_output`` payload is the emit tool's error marker."""
    return isinstance(payload, dict) and _VALIDATION_ERROR_KEY in payload


_request_model = structured_ns.model(
    "StructuredRequest",
    {
        "query": fields.String(required=True, description="Natural-language request"),
        "schema": fields.Raw(required=True, description="JSON Schema for the output object"),
        "workspace": fields.String(required=False, description="Wiki slug / SCG scope"),
        "tools": fields.List(fields.String, required=False, description="Tool allowlist"),
    },
)


@structured_ns.route("")
class StructuredResource(Resource):
    """Kick off a schema-constrained agentic synthesis and return a run handle."""

    @structured_ns.expect(_request_model)
    def post(self) -> tuple[dict, int]:
        """Validate the request, start the run async, short-await a fast result."""
        if (auth := _require_api_key()) is not None:
            return auth
        if _runtime is None:
            return {"message": "Structured response not initialized"}, 503
        data = request.get_json(silent=True) or {}
        query = data.get("query")
        schema = data.get("schema")
        if not query or not isinstance(query, str):
            return {"message": "Invalid input: 'query' (string) is required"}, 400
        if not isinstance(schema, dict):
            return {"message": "Invalid input: 'schema' (JSON Schema object) is required"}, 400
        workspace = data.get("workspace") if isinstance(data.get("workspace"), str) else None
        raw_tools = data.get("tools")
        tools = [str(t) for t in raw_tools if t] if isinstance(raw_tools, list) else None

        responder = StructuredResponder(
            runtime=_runtime,
            schema=schema,
            workspace=workspace,
            allowed_tools=tools,
        )
        try:
            run_id = responder.start_async(query)
        except StructuredResponseError as exc:
            return _error(422, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            logging.warning("structured start failed: {}", exc)
            return _error(500, f"structured run failed to start: {exc}")
        if not run_id:
            # Registry refused the start (a run is already active on this session).
            return _error(409, "a structured run is already active for this session")

        session_id = _session_id_of(run_id)
        output = self._await_fast_output(session_id)
        if output is not None and not _is_validation_error_payload(output):
            return {
                "run_id": run_id,
                "status": "completed",
                "output": output,
                "workspace": workspace,
            }, 200
        return {"run_id": run_id, "status": "running", "workspace": workspace}, 200

    @staticmethod
    def _await_fast_output(session_id: str) -> object | None:
        """Poll the transcript briefly for a fast completion; None if not ready."""
        deadline = time.monotonic() + _FAST_AWAIT_SECONDS
        while True:
            try:
                output = _load_structured_output(session_id)
            except Exception as exc:  # noqa: BLE001 — fast-path is best-effort
                logging.debug("fast-await transcript read failed: {}", exc)
                return None
            if output is not None:
                return output
            if time.monotonic() >= deadline:
                return None
            time.sleep(_FAST_AWAIT_INTERVAL)


@structured_ns.route("/<path:run_id>")
class StructuredRunResource(Resource):
    """Resolve a run handle to its latest structured output or status."""

    def get(self, run_id: str) -> tuple[dict, int]:
        """Return ``{run_id, status, output?, error?}`` for a run handle."""
        if (auth := _require_api_key()) is not None:
            return auth
        if _runtime is None:
            return {"message": "Structured response not initialized"}, 503
        session_id = _session_id_of(run_id)
        # Unknown run id must 404, not fall through to the phantom idle/422 branch
        # (#40/#64). ``summarize_session`` never raises for an unknown id — it
        # returns ``status:"idle"`` (∉ _TERMINAL_STATUSES) → a misleading running
        # 200 / 422. Check existence FIRST so a genuinely-unknown run is a clean
        # 404 while a real failed run still 422s and a running one stays running.
        if session_id not in _runtime.session_store.list_sessions():
            return {"run_id": run_id, **_error(404, f"run {run_id} not found")[0]}, 404
        try:
            output = _load_structured_output(session_id)
            status = str(_runtime.summarize_session(session_id).get("status", "running"))
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            logging.warning("structured status read failed: {}", exc)
            return {"run_id": run_id, **_error(404, f"run {run_id} not found")[0]}, 404

        if output is not None and not _is_validation_error_payload(output):
            # Output present → always report as completed regardless of the raw
            # summarize_session status.  The emit tool only fires on success, so
            # any payload that passes the validation-error gate IS a completed
            # run, even if the session is still technically "running" (brief race
            # between the transcript append and the session-end event).
            return {"run_id": run_id, "status": "completed", "output": output}, 200

        if output is not None and _is_validation_error_payload(output):
            # The emit tool gave up after the reask cap — a structured failure.
            return {"run_id": run_id, "status": status, **_error(422, _NO_RESULT_REASON)[0]}, 422

        if status in _TERMINAL_STATUSES:
            # Terminal with no structured_output → the model never produced one.
            return {"run_id": run_id, "status": status, **_error(422, _NO_RESULT_REASON)[0]}, 422

        # Still running, no output yet.
        return {"run_id": run_id, "status": status}, 200
