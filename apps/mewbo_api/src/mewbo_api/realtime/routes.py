"""Realtime token-streaming draft synthesis — ``POST /v1/draft/stream``.

Token-streaming path: the
:class:`~mewbo_core.draft_stream.DraftStreamer` bridges one tool-light
``.astream()`` of LLM token deltas to Flask's sync WSGI as server-sent events,
ending with an additive terminal ``done`` frame carrying the backing
``session_id`` (also sent up front in the ``X-Mewbo-Session`` header).

**Session-full with write-behind (#78).** The draft path was sessionless by
design; that was reclassified as a defect. It now mints a real session, runs the
LLM inside its Langfuse trace, and persists the single-turn transcript via
:class:`~mewbo_api.realtime.recorder.RealtimeSessionRecorder` AFTER the last token
— so the TTFT path never gains a blocking store write. The wire contract is
unchanged except for the additive ``session_id`` (terminal ``done`` frame +
``X-Mewbo-Session`` header).

Auth mirrors ``mewbo_api.structured.routes.init_structured``:
``require_api_key`` is injected by the controller in ``backend.py``.

The namespace is mounted at ``/v1/draft`` (``draft_ns``) → ``/v1/draft/stream``.

The no-loop, retrieval-only structured synthesis lane (formerly the sibling
``POST /v1/structured/fast``) now lives as ``mode: "synthesis"`` ON the agentic
``POST /v1/structured`` endpoint (#85) — see ``mewbo_api.structured.synthesis``,
which reuses the shared :class:`RealtimeSessionRecorder` + ``WikiGroundingProvider``
glue this package owns.
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
from mewbo_core.structured_synthesis import _format_citations

from mewbo_api.realtime.recorder import RealtimeSessionRecorder
from mewbo_api.request_context import request_surface
from mewbo_api.responses import ApiResponseKit

logging = get_logger(name="api.realtime.routes")

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]


def _no_auth() -> AuthResult:
    return None


_require_api_key: AuthGuard = _no_auth
_runtime: Any = None


def init_realtime(api: object, require_api_key: AuthGuard, runtime: Any = None) -> None:
    """Wire the ``/v1/draft/stream`` endpoint.

    Args:
        api: The :class:`flask_restx.Api` instance (same object passed to
            ``init_structured``).
        require_api_key: Auth guard injected by the controller; ``None`` return
            means "authorised".
        runtime: Session runtime (session store seam). Used to session-back the
            stream with write-behind persistence (#78); ``None`` degrades
            gracefully to trace-only (no transcript persisted).

    This is the ONE line the controller must add in ``backend.py``::

        from mewbo_api.realtime import init_realtime
        init_realtime(api, require_api_key, runtime)
    """
    global _require_api_key, _runtime
    _require_api_key = require_api_key
    _runtime = runtime
    api.add_namespace(draft_ns, path="/v1/draft")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Draft streaming namespace — POST /v1/draft/stream
# ---------------------------------------------------------------------------

draft_ns = Namespace(
    "draft",
    description="Token-streaming draft answers over server-sent events.",
)

# Error-envelope/message examples for this namespace (the draft route only emits
# the legacy ``{"message": ...}`` shape, but the kit is the one DRY home). Built
# at module level so the import-time decorators can see it; ``Draft`` prefix
# namespaces the generated model names on the shared Api registry.
kit = ApiResponseKit(draft_ns, prefix="Draft")

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

    @draft_ns.doc(
        description=(
            "Stream a draft answer as **server-sent events** (`text/event-stream`). "
            "The backing session id is sent up front in the `X-Mewbo-Session` "
            "response header.\n\n"
            "**Event frames:**\n\n"
            "- `data: {\"token\": \"<delta>\"}` — one per LLM token delta, in order.\n"
            "- `data: {\"done\": true, \"session_id\": \"<id>\"}` — terminal success "
            "frame; carries the backing session id so a streaming caller can resolve "
            "the trace.\n"
            "- `data: {\"error\": \"<reason>\"}` — emitted instead of `done` on a "
            "mid-stream failure.\n\n"
            "Pass an optional `workspace` (wiki slug) to add retrieval grounding "
            "before streaming starts, or `model` to override the configured default "
            "with any LiteLLM model id (a non-string value is ignored). Consume with "
            "an SSE client (e.g. `EventSource`), not a buffered JSON reader."
        )
    )
    @draft_ns.expect(_draft_request_model)
    @draft_ns.response(
        200,
        "Server-sent event stream of token frames (token / done / error); see description.",
    )
    @kit.errors(400, shape="message")
    @kit.auth_error()
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


__all__ = ["init_realtime", "draft_ns"]
