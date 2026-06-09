"""Realtime fast-grounded structured synthesis — ``POST /v1/structured/fast``.

SESSIONLESS path: no session store, no transcript, no ``Orchestrator``.  The
:class:`~mewbo_core.structured_synthesis.StructuredSynthesizer` drives a single
async LLM round-trip (+ one optional reask) and returns immediately with:

    {
        "output": <validated-payload>,
        "citations": [{"id", "kind", "snippet", "score", "source"}, ...],
        "status": "completed"
    }

Also provides the token-streaming draft endpoint — ``POST /v1/draft/stream``:

    POST /v1/draft/stream  → SSE of LLM token deltas (tool-light, no session)

Auth mirrors ``mewbo_api.structured.routes.init_structured``:
``require_api_key`` is injected by the controller in ``backend.py``.

The namespaces are mounted at:
    /v1/structured  (realtime_ns) → /v1/structured/fast
    /v1/draft       (draft_ns)    → /v1/draft/stream

These coexist alongside the agentic path:

    POST /v1/structured       → agentic, session-backed (StructuredResponder)
    POST /v1/structured/fast  → retrieval-only, sessionless (StructuredSynthesizer)
    POST /v1/draft/stream     → token-streaming, tool-light (DraftStreamer)
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from flask import Response, request, stream_with_context
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.draft_stream import DraftStreamer
from mewbo_core.structured_response import StructuredResponseError
from mewbo_core.structured_synthesis import StructuredSynthesizer, _format_citations

logging = get_logger(name="api.realtime.routes")

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth
_runtime: Any = None

# Re-use the parent namespace declared in mewbo_api.structured.routes; we
# *cannot* re-declare a Namespace with the same path — instead init_realtime
# is given the ALREADY-CREATED namespace object from the caller so it can
# register an additional Resource on it.
#
# However, a Namespace *must* exist at construction time for the ``@ns.route``
# decorator, so we create our own that is mounted at a sub-path relative to the
# parent.  The controller calls ``api.add_namespace(realtime_ns, path="/v1/structured")``
# which mounts it alongside the existing structured namespace — Flask-RESTX
# merges resources on the same root path, so /fast resolves correctly.
realtime_ns = Namespace(
    "realtime",
    description="Retrieval-only fast grounded structured synthesis.",
)


def init_realtime(api: object, require_api_key: AuthGuard, runtime: Any = None) -> None:
    """Wire the ``/v1/structured/fast`` and ``/v1/draft/stream`` endpoints.

    Args:
        api: The :class:`flask_restx.Api` instance (same object passed to
            ``init_structured``).
        require_api_key: Auth guard injected by the controller; ``None`` return
            means "authorised".
        runtime: Optional session runtime (unused by this sessionless path but
            accepted for parity with ``init_structured`` so the controller can
            pass the same args to both).

    This is the ONE line the controller must add in ``backend.py``::

        from mewbo_api.realtime import init_realtime
        init_realtime(api, require_api_key, runtime)
    """
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(realtime_ns, path="/v1/structured")  # type: ignore[attr-defined]
    api.add_namespace(draft_ns, path="/v1/draft")  # type: ignore[attr-defined]


def _error(code: int, reason: str) -> tuple[dict, int]:
    """Canonical structured error envelope."""
    return {"error": {"code": code, "reason": reason}}, code


_request_model = realtime_ns.model(
    "FastStructuredRequest",
    {
        "query": fields.String(required=True, description="Natural-language request"),
        "schema": fields.Raw(required=True, description="JSON Schema for the output object"),
        "workspace": fields.String(
            required=False, description="Wiki slug (optional grounding scope)"
        ),
    },
)

_citation_model = realtime_ns.model(
    "Citation",
    {
        "id": fields.String(description="Source identifier"),
        "kind": fields.String(description="Source kind (page / node / memory)"),
        "snippet": fields.String(description="Extracted text excerpt"),
        "score": fields.Float(description="Retrieval relevance score"),
        "source": fields.String(description="Human-readable source label"),
    },
)

_response_model = realtime_ns.model(
    "FastStructuredResponse",
    {
        "output": fields.Raw(description="Schema-validated structured output"),
        "citations": fields.List(fields.Nested(_citation_model)),
        "status": fields.String(description="Always 'completed' on success"),
    },
)


@realtime_ns.route("/fast")
class FastStructuredResource(Resource):
    """Single-round-trip schema-constrained synthesis with retrieval grounding."""

    @realtime_ns.expect(_request_model)
    @realtime_ns.response(200, "Completed", _response_model)
    @realtime_ns.response(400, "Bad request")
    @realtime_ns.response(401, "Unauthorized")
    @realtime_ns.response(422, "Synthesis failed")
    def post(self) -> tuple[dict, int]:
        """Retrieve grounding, synthesize, validate, return immediately."""
        if (auth := _require_api_key()) is not None:
            return auth

        data = request.get_json(silent=True) or {}
        query = data.get("query")
        schema = data.get("schema")
        if not query or not isinstance(query, str):
            return {"message": "Invalid input: 'query' (string) is required"}, 400
        if not isinstance(schema, dict):
            return {"message": "Invalid input: 'schema' (JSON Schema object) is required"}, 400

        workspace = data.get("workspace")
        if not isinstance(workspace, str):
            workspace = None

        # Import the concrete grounding provider here (optional dep, lazy).
        try:
            from mewbo_api.realtime.grounding import WikiGroundingProvider  # noqa: PLC0415
            grounding_provider: WikiGroundingProvider | None = WikiGroundingProvider()
        except Exception as exc:  # noqa: BLE001
            logging.debug("WikiGroundingProvider unavailable: {}", exc)
            grounding_provider = None

        synthesizer = StructuredSynthesizer(
            grounding_provider=grounding_provider,
        )

        try:
            payload, citations = asyncio.run(
                synthesizer.synthesize(query, schema, workspace=workspace)
            )
        except StructuredResponseError as exc:
            return _error(422, str(exc))
        except Exception as exc:  # noqa: BLE001
            logging.warning("FastStructuredResource synthesis failed: {}", exc)
            return _error(500, f"synthesis failed: {exc}")

        citations_out = [
            {
                "id": c.id,
                "kind": c.kind,
                "snippet": c.snippet,
                "score": c.score,
                "source": c.source,
            }
            for c in citations
        ]
        return {
            "output": payload,
            "citations": citations_out,
            "status": "completed",
        }, 200


# ---------------------------------------------------------------------------
# Draft streaming namespace — POST /v1/draft/stream
# ---------------------------------------------------------------------------

draft_ns = Namespace(
    "draft",
    description="Token-streaming LLM draft synthesis (tool-light, no session).",
)

_draft_request_model = draft_ns.model(
    "DraftStreamRequest",
    {
        "query": fields.String(required=True, description="Natural-language request"),
        "workspace": fields.String(
            required=False, description="Wiki slug for optional grounding (omit for no grounding)"
        ),
        "model": fields.String(
            required=False, description="LiteLLM model name override (omit for configured default)"
        ),
    },
)

_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-API-KEY",
}


@draft_ns.route("/stream")
class DraftStreamResource(Resource):
    r"""Token-streaming synthesis — ``POST /v1/draft/stream``.

    Returns a ``text/event-stream`` response where each frame is::

        data: {"token": "<delta>"}\n\n

    terminated by::

        data: {"done": true}\n\n

    The async generator is bridged to Flask's sync WSGI via a single dedicated
    event loop (per-request, single-shot — no thread-per-session overhead).
    """

    @draft_ns.expect(_draft_request_model)
    @draft_ns.response(200, "SSE stream of token deltas")
    @draft_ns.response(400, "Bad request")
    @draft_ns.response(401, "Unauthorized")
    def post(self) -> Response:
        """Stream LLM token deltas for a natural-language query."""
        if (auth := _require_api_key()) is not None:
            return auth  # type: ignore[return-value]

        data = request.get_json(silent=True) or {}
        query = data.get("query")
        if not query or not isinstance(query, str):
            return {"message": "Invalid input: 'query' (string) is required"}, 400  # type: ignore[return-value]

        workspace = data.get("workspace")
        if not isinstance(workspace, str):
            workspace = None

        model_override = data.get("model")
        if not isinstance(model_override, str):
            model_override = None

        # Optional grounding: fetch citations + format as context string.
        context = ""
        if workspace:
            try:
                from mewbo_api.realtime.grounding import WikiGroundingProvider  # noqa: PLC0415
                cites = WikiGroundingProvider().search(workspace, query)
                context = _format_citations(cites)
            except Exception as exc:  # noqa: BLE001
                logging.debug("WikiGroundingProvider unavailable in draft/stream: {}", exc)

        streamer = DraftStreamer(model_name=model_override)

        def _generate():
            """Bridge async astream to sync Flask generator — single event loop."""
            loop = asyncio.new_event_loop()
            try:
                agen = streamer.astream(query, context=context)
                while True:
                    try:
                        delta = loop.run_until_complete(agen.__anext__())
                    except StopAsyncIteration:
                        break
                    yield f"data: {json.dumps({'token': delta})}\n\n"
                yield f"data: {json.dumps({'done': True})}\n\n"
            finally:
                loop.close()

        headers = {
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            **_CORS_HEADERS,
        }
        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers=headers,
        )


__all__ = ["init_realtime", "realtime_ns", "draft_ns"]
