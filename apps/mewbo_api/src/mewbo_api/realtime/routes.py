"""Realtime fast-grounded structured synthesis — ``POST /v1/structured/fast``.

Single-round-trip path: the
:class:`~mewbo_core.structured_synthesis.StructuredSynthesizer` drives one async
LLM call (+ one optional reask) and returns immediately with:

    {
        "output": <validated-payload>,
        "citations": [{"id", "kind", "snippet", "score", "source"}, ...],
        "status": "completed",
        "session_id": <id>            # additive (#78)
    }

Also provides the token-streaming draft endpoint — ``POST /v1/draft/stream``:

    POST /v1/draft/stream  → SSE of LLM token deltas (tool-light)

**Session-full with write-behind (#78).** Both paths were sessionless by design;
that was reclassified as a defect. They now mint a real session, run the LLM
inside its Langfuse trace, and persist the single-turn transcript via
:class:`~mewbo_api.realtime.recorder.RealtimeSessionRecorder` AFTER the response /
last token — so the draft TTFT path never gains a blocking store write. The wire
contract is unchanged except for the additive ``session_id`` (fast response body,
draft terminal ``done`` frame + ``X-Mewbo-Session`` header).

Auth mirrors ``mewbo_api.structured.routes.init_structured``:
``require_api_key`` is injected by the controller in ``backend.py``.

The namespaces are mounted at:
    /v1/structured  (realtime_ns) → /v1/structured/fast
    /v1/draft       (draft_ns)    → /v1/draft/stream

These coexist alongside the agentic path:

    POST /v1/structured       → agentic, session-backed (StructuredResponder)
    POST /v1/structured/fast  → retrieval-only, fast (StructuredSynthesizer)
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

from mewbo_api.realtime.recorder import RealtimeSessionRecorder
from mewbo_api.request_context import request_surface

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
        runtime: Session runtime (session store seam). Used by both paths to
            session-back the run with write-behind persistence (#78); ``None``
            degrades gracefully to trace-only (no transcript persisted).

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
        "query": fields.String(
            required=True,
            description="Natural-language request to answer.",
            example="Which services does the deploy pipeline restart?",
        ),
        "schema": fields.Raw(
            required=True,
            description="JSON Schema the output object must validate against.",
            example={
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        ),
        "workspace": fields.String(
            required=False,
            description="Wiki slug used for retrieval grounding. Omit for no grounding.",
            example="my-project",
        ),
        "model": fields.String(
            required=False,
            description=(
                "Optional model override. Any configured LiteLLM model id; a non-string "
                "value is ignored and the configured default is used."
            ),
            example="openai/gpt-5.4-nano",
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
        "session_id": fields.String(
            description="Session id backing this run (additive; for trace/transcript lookup)"
        ),
    },
)


@realtime_ns.route("/fast")
class FastStructuredResource(Resource):
    """Single-round-trip schema-constrained synthesis with retrieval grounding."""

    @realtime_ns.expect(_request_model)
    @realtime_ns.response(200, "Validated output with grounding citations", _response_model)
    @realtime_ns.response(400, "Missing or invalid query or schema")
    @realtime_ns.response(401, "Missing or invalid API key")
    @realtime_ns.response(422, "The answer failed schema validation after one reask")
    @realtime_ns.response(500, "Synthesis failed unexpectedly")
    def post(self) -> tuple[dict, int]:
        """Run a fast structured query.

        Answers the query in one round trip with a JSON object that validates
        against the supplied schema; no tools are used. A workspace adds retrieval
        grounding, and the matching citations come back alongside the output. An
        answer that fails schema validation is reasked once; a second failure
        returns 422. The optional `model` field accepts any configured LiteLLM
        model id; a non-string value is ignored. The response also carries the
        `session_id` backing the run, for trace and transcript lookup.
        """
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

        # Optional LiteLLM model override (e.g. ``openai/gpt-5.4-nano``); a
        # non-string is ignored → the configured default is used. Mirrors the
        # draft-route idiom.
        model_override = data.get("model")
        if not isinstance(model_override, str):
            model_override = None

        # Import the concrete grounding provider here (optional dep, lazy).
        try:
            from mewbo_api.realtime.grounding import WikiGroundingProvider  # noqa: PLC0415
            grounding_provider: WikiGroundingProvider | None = WikiGroundingProvider()
        except Exception as exc:  # noqa: BLE001
            logging.debug("WikiGroundingProvider unavailable: {}", exc)
            grounding_provider = None

        synthesizer = StructuredSynthesizer(
            model_name=model_override,
            grounding_provider=grounding_provider,
        )

        # Session-back the run (#78): mint a session, run the synthesis inside its
        # Langfuse trace, then persist write-behind AFTER the response is built.
        recorder = RealtimeSessionRecorder.for_fast(
            _runtime,
            query,
            surface=request_surface(),
            workspace=workspace,
            model=model_override,
        )
        try:
            with recorder.trace():
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
        # Write-behind: the response is fully built; persistence never blocks it.
        if _runtime is not None:
            recorder.persist_async(output=payload)
        return {
            "output": payload,
            "citations": citations_out,
            "status": "completed",
            "session_id": recorder.session_id,
        }, 200


# ---------------------------------------------------------------------------
# Draft streaming namespace — POST /v1/draft/stream
# ---------------------------------------------------------------------------

draft_ns = Namespace(
    "draft",
    description="Token-streaming draft answers over server-sent events.",
)

_draft_request_model = draft_ns.model(
    "DraftStreamRequest",
    {
        "query": fields.String(
            required=True,
            description="Natural-language request to answer.",
            example="Draft a short release note for the latest deploy.",
        ),
        "workspace": fields.String(
            required=False,
            description="Wiki slug used for retrieval grounding. Omit for no grounding.",
            example="my-project",
        ),
        "model": fields.String(
            required=False,
            description=(
                "Optional model override. Any configured LiteLLM model id; a non-string "
                "value is ignored and the configured default is used."
            ),
            example="openai/gpt-5.4-nano",
        ),
    },
)

# CORS allow-origin/headers/methods are owned by ``backend._add_cors_headers``
# (``after_request``, the single seam — it already lists ``X-Mewbo-Surface``); do
# NOT re-declare them here or the two drift. We only need to EXPOSE the additive
# ``X-Mewbo-Session`` response header so cross-origin JS can read it (the id also
# rides the terminal SSE frame, so SSE consumers don't depend on this).
_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Access-Control-Expose-Headers": "X-Mewbo-Session",
}


@draft_ns.route("/stream")
class DraftStreamResource(Resource):
    """Token-streaming draft synthesis over server-sent events.

    The async token generator is bridged to Flask's sync WSGI via a single
    dedicated event loop (per-request, single-shot; no thread-per-session
    overhead). The wire frames are documented on the POST method below.
    """

    @draft_ns.expect(_draft_request_model)
    @draft_ns.response(200, "Server-sent event stream of token frames.")
    @draft_ns.response(400, "Missing or invalid query")
    @draft_ns.response(401, "Missing or invalid API key")
    def post(self) -> Response:
        """Stream a draft answer.

        Streams a draft answer as server-sent events. Each token arrives as a
        `data: {"token": "<delta>"}` frame, and the stream ends with a terminal
        `data: {"done": true, "session_id": "<id>"}` frame; a mid-flight failure
        emits a `data: {"error": "<reason>"}` frame instead. The backing session
        id is also sent up front in the `X-Mewbo-Session` response header. A
        workspace adds retrieval grounding before streaming starts. The optional
        `model` field accepts any configured LiteLLM model id; a non-string
        value is ignored.
        """
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

        # Session-back the stream (#78): mint a session, stream inside its Langfuse
        # trace, persist write-behind from the generator tail (after the last
        # token is yielded) so TTFT never pays for a store write.
        recorder = RealtimeSessionRecorder.for_draft(
            _runtime,
            query,
            surface=request_surface(),
            workspace=workspace,
            model=model_override,
        )

        def _generate():
            """Bridge async astream to sync Flask generator — single event loop."""
            loop = asyncio.new_event_loop()
            chunks: list[str] = []
            error: str | None = None
            try:
                with recorder.trace():
                    agen = streamer.astream(query, context=context)
                    while True:
                        try:
                            delta = loop.run_until_complete(agen.__anext__())
                        except StopAsyncIteration:
                            break
                        chunks.append(delta)
                        yield f"data: {json.dumps({'token': delta})}\n\n"
            except Exception as exc:  # noqa: BLE001
                # A mid-stream failure must be honest on BOTH surfaces: an SSE
                # error frame for the client, and an ``error`` completion so the
                # transcript summarizes as ``failed``, never a false ``completed``.
                error = str(exc)
                logging.warning("draft/stream failed mid-stream: {}", exc)
                yield f"data: {json.dumps({'error': error})}\n\n"
            else:
                # ``session_id`` rides the terminal frame (additive — token frames
                # are unchanged) so a streaming caller can still resolve the trace.
                yield f"data: {json.dumps({'done': True, 'session_id': recorder.session_id})}\n\n"
            finally:
                loop.close()
                # Write-behind: persist off the latency path on a daemon thread so
                # the connection closes without waiting on store writes.
                if _runtime is not None:
                    recorder.persist_async(text="".join(chunks), error=error)

        headers = {**_SSE_HEADERS, "X-Mewbo-Session": recorder.session_id}
        return Response(
            stream_with_context(_generate()),
            mimetype="text/event-stream",
            headers=headers,
        )


__all__ = ["init_realtime", "realtime_ns", "draft_ns"]
