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

import dataclasses
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
from pydantic import BaseModel, Field

from mewbo_api.request_context import request_surface
from mewbo_api.responses import ApiResponseKit
from mewbo_api.structured.synthesis import SynthesisRunner


class RunProvenance(BaseModel):
    """Graph-first pathway/probe provenance for a structured run (#77).

    The additive audit trail a graph-first ``/v1/structured`` run surfaces in its
    GET payload — the story "graph consulted → probes executed → emit". Pure
    projection of the session transcript (``scg_route`` tool results +
    ``sub_agent`` lifecycle), so it is typed wire, not a bag of dicts. Absent
    (``None``) for a plain/wiki-grounded run that fanned no probes.
    """

    recipes_routed: int = Field(
        0, description="Count of scg_route calls that proposed pathways."
    )
    probes_run: int = Field(0, description="Distinct probe sub-agents spawned.")
    probe_status: dict[str, str] = Field(
        default_factory=dict,
        description="Per-probe agent_id → terminal status (or 'running').",
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

# One DRY home for the ``{error: {code, reason}}`` envelope examples this module
# returns (built at module level so the import-time decorators can see it). A
# unique ``Structured`` prefix namespaces the generated model names on the shared
# Api registry.
kit = ApiResponseKit(structured_ns, prefix="Structured")


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
    return _structured_output_from(_runtime.session_store.load_transcript(session_id))


def _structured_output_from(events: list[dict[str, Any]]) -> object | None:
    """The LATEST ``structured_output`` payload in an already-loaded transcript."""
    payload: object | None = None
    for event in events:
        if event.get("type") == STRUCTURED_OUTPUT_EVENT:
            payload = event.get("payload")
    return payload


def _is_validation_error_payload(payload: object) -> bool:
    """True when a ``structured_output`` payload is the emit tool's error marker."""
    return isinstance(payload, dict) and _VALIDATION_ERROR_KEY in payload


def _provenance_from(events: list[dict[str, Any]]) -> RunProvenance | None:
    """Summarize the graph-first probe fan-out from an already-loaded transcript.

    Reconstructs the pathway/probe provenance for a graph-first structured run
    (#77) from the same transcript the result is read from: which probe
    sub-agents ran (``sub_agent`` events) and how many ``scg_route`` calls routed
    pathways (``tool_result`` events). ADDITIVE — returns ``None`` for a
    non-graph (wiki-grounded or plain) run that fanned no probes, so the wire
    shape only carries provenance when there is something to carry.
    """
    probes: dict[str, str] = {}
    routes = 0
    for event in events:
        etype = event.get("type")
        raw = event.get("payload")
        payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
        if etype == "sub_agent":
            agent_id = str(payload.get("agent_id") or "")
            if agent_id and str(payload.get("action")) == "stop":
                probes[agent_id] = str(payload.get("status") or "completed")
            elif agent_id:
                probes.setdefault(agent_id, "running")
        elif etype == "tool_result" and str(payload.get("tool_id")) == "scg_route":
            routes += 1
    if not probes and not routes:
        return None
    return RunProvenance(
        recipes_routed=routes, probes_run=len(probes), probe_status=probes
    )


_request_model = structured_ns.model(
    "StructuredQueryRequest",
    {
        "query": fields.String(
            required=True,
            description="Natural-language request to answer.",
            example="List the public HTTP endpoints and what each one returns.",
        ),
        "schema": fields.Raw(
            required=True,
            description="JSON Schema the output object must validate against.",
            example={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "endpoints": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary"],
            },
        ),
        "workspace": fields.String(
            required=False,
            description=(
                "Wiki slug or search workspace that grounds the run. A workspace mapped "
                "to an indexed search workspace also grants graph traversal tools."
            ),
            example="my-project",
        ),
        "tools": fields.List(
            fields.String,
            required=False,
            description="Allowlist of tool ids the run may use. Omit for the default set.",
            example=["wiki_search"],
        ),
        "model": fields.String(
            required=False,
            description=(
                "Optional model override. Any configured LiteLLM model id; a non-string "
                "value is ignored and the configured default is used."
            ),
            example="openai/gpt-5.4-nano",
        ),
        "mode": fields.String(
            required=False,
            enum=["agentic", "synthesis"],
            description=(
                "Execution strategy. Omit (default 'agentic') for the tool-using, "
                "session-backed run that returns a run handle to poll. 'synthesis' "
                "selects a no-loop, retrieval-only single round-trip (no tools): it "
                "returns inline with status 'completed', the validated output, and "
                "grounding citations — lower latency for cheap structured extraction."
            ),
            example="synthesis",
        ),
    },
)


# Success-response models. ``example=`` is what Scalar synthesizes the sample
# body from, so each field carries a realistic value matching the handler's
# actual ``return`` shape.
_provenance_model = structured_ns.model(
    "StructuredRunProvenance",
    {
        "recipes_routed": fields.Integer(
            example=2,
            description="Count of scg_route calls that proposed pathways.",
        ),
        "probes_run": fields.Integer(
            example=3, description="Distinct probe sub-agents spawned."
        ),
        "probe_status": fields.Raw(
            example={"a-r1a2-1": "completed", "a-r1a2-2": "completed"},
            description="Per-probe agent_id → terminal status (or 'running').",
        ),
    },
)

_run_handle_model = structured_ns.model(
    "StructuredRunHandle",
    {
        "run_id": fields.String(
            example="9e2d47c1f0:r1",
            description=(
                "Run handle of the form <session_id>:r<seq>; "
                "poll GET /v1/structured/{run_id}."
            ),
        ),
        "status": fields.String(
            example="completed",
            description="'completed' when the output is attached inline, else 'running'.",
        ),
        "output": fields.Raw(
            example={
                "summary": "The API exposes 4 public HTTP endpoints.",
                "endpoints": ["POST /v1/structured", "GET /v1/structured/{run_id}"],
            },
            description=(
                "The schema-validated result object — present only once the run has completed."
            ),
        ),
        "workspace": fields.String(
            example="my-project",
            description="The grounding workspace echoed from the request (may be null).",
        ),
        "citations": fields.List(
            fields.Raw,
            description="Grounding citations (synthesis mode only).",
        ),
    },
)

_run_status_model = structured_ns.model(
    "StructuredRunStatus",
    {
        "run_id": fields.String(example="9e2d47c1f0:r1"),
        "status": fields.String(
            example="completed",
            description="Run state: 'completed', 'running', or a terminal session status.",
        ),
        "output": fields.Raw(
            example={
                "summary": "The API exposes 4 public HTTP endpoints.",
                "endpoints": ["POST /v1/structured", "GET /v1/structured/{run_id}"],
            },
            description="The schema-validated result object, attached once the run has completed.",
        ),
        "provenance": fields.Nested(
            _provenance_model,
            description=(
                "Graph-first pathway/probe provenance (additive; absent for plain/wiki runs)."
            ),
        ),
    },
)


@structured_ns.route("")
class StructuredResource(Resource):
    """Kick off a schema-constrained agentic synthesis and return a run handle."""

    @structured_ns.doc(
        description=(
            "Run a schema-constrained, tool-using structured query and get back a "
            "JSON object that validates against your `schema`.\n\n"
            "**Two execution modes** (set via the `mode` field):\n\n"
            "- **`agentic`** (default) — kicks off a session-backed, tool-using run "
            "and returns a **run handle** of the form `<session_id>:r<seq>`. The "
            "request waits a few seconds, so fast runs come back inline with "
            "`status:\"completed\"` and `output` attached; slower runs return "
            "`status:\"running\"` — poll `GET /v1/structured/{run_id}` until it "
            "completes, or attach to `GET /api/sessions/{session_id}/stream` using "
            "the part of the handle before the first `:`.\n"
            "- **`synthesis`** — a no-loop, retrieval-only single round-trip (no "
            "tools). Returns **inline** with `status:\"completed\"`, the validated "
            "`output`, and grounding `citations` — lower latency for cheap "
            "structured extraction.\n\n"
            "`workspace` grounds the run on a wiki slug or a search workspace (a "
            "mapped search workspace also grants graph traversal tools, making the "
            "agentic run graph-first). `model` overrides the configured default "
            "with any LiteLLM model id; a non-string value is ignored.\n\n"
            "Example: `{\"query\": \"List the public endpoints\", \"schema\": "
            "{\"type\": \"object\", ...}, \"mode\": \"synthesis\"}`."
        )
    )
    @structured_ns.expect(_request_model)
    @structured_ns.response(
        200,
        "Run handle (agentic) or inline result (synthesis); output attached when completed",
        _run_handle_model,
    )
    @kit.errors(409, 422, 500)
    @kit.errors(400, 503, shape="message")
    @kit.auth_error()
    def post(self) -> tuple[dict, int]:
        """Run a structured query.

        Starts an agentic run that answers the query with a JSON object validating
        against the supplied schema. The response always carries a run handle of the
        form `<session_id>:r<seq>`. The request waits briefly before returning, so
        fast runs come back inline with status `completed` and the output attached.
        Slower runs return status `running`: poll GET /v1/structured/{run_id}, or
        attach to GET /api/sessions/{session_id}/stream using the part of the handle
        before the first colon. The optional `model` field accepts any configured
        LiteLLM model id; a non-string value is ignored. Set `mode` to `synthesis`
        for a no-loop, retrieval-only single round-trip (no tools) that returns
        inline with status `completed` plus grounding citations — lower latency for
        cheap structured extraction.
        """
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
        # Optional LiteLLM model override (e.g. ``openai/gpt-5.4-nano``); a
        # non-string is ignored → the configured default is used. Matches the
        # draft-route idiom (``/v1/draft/stream``).
        model = data.get("model") if isinstance(data.get("model"), str) else None

        # ``mode: "synthesis"`` selects the no-loop, single-round-trip strategy
        # (the former /v1/structured/fast lane, folded in by #85): a synchronous
        # StructuredSynthesizer call instead of the agentic ToolUseLoop — ~1–3s,
        # retrieval-only, no tools. Anything else (incl. the default 'agentic')
        # takes the tool-using, session-backed path below.
        if isinstance(data.get("mode"), str) and data["mode"] == "synthesis":
            return self._run_synthesis(query, schema, workspace, model)

        responder = self._build_responder(schema, workspace, tools, model)
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
    def _run_synthesis(
        query: str,
        schema: dict[str, Any],
        workspace: str | None,
        model: str | None,
    ) -> tuple[dict, int]:
        """Run the no-loop synthesis mode and shape its response/error envelope.

        The single round-trip completes inline, so the body carries the validated
        ``output`` + grounding ``citations`` with status ``completed`` (no polling
        needed); the ``<session_id>:r1`` run handle still resolves via
        ``GET /v1/structured/<run_id>`` once the write-behind persist lands. A
        schema-validation failure after the bounded reask is a 422 envelope; any
        other failure is a 500 — same canonical ``{error: {code, reason}}`` shape
        as the agentic path, never a raw exception or the internal tool name.
        """
        try:
            body = SynthesisRunner(runtime=_runtime).run(
                query=query,
                schema=schema,
                workspace=workspace,
                model=model,
                surface=request_surface(),
            )
        except StructuredResponseError as exc:
            return _error(422, str(exc))
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            logging.warning("structured synthesis failed: {}", exc)
            return _error(500, f"structured synthesis failed: {exc}")
        return body, 200

    @staticmethod
    def _build_responder(
        schema: dict[str, Any],
        workspace: str | None,
        tools: list[str] | None,
        model: str | None = None,
    ) -> StructuredResponder:
        """Build the structured responder, routing graph-first when eligible.

        When ``workspace`` resolves to a mapped Agentic Search workspace and SCG
        is enabled, the run goes GRAPH-FIRST (#77): the same agentic session, but
        granted the ``scg`` capability + graph traversal tools + the workspace
        source scope, driven by the ``scg-search-structured`` playbook so it
        routes → fans probes out → aggregates → emits a schema-validated object.
        Otherwise (a wiki slug, an unmapped/unknown workspace, or SCG off) the
        default wiki-grounded ``StructuredResponder`` path is used — the wire
        shape is identical either way, the graph-first provenance is additive.

        ``model`` (an optional LiteLLM name) overrides the configured default for
        this run. It is applied at the ONE route seam below so it covers BOTH
        paths: the default responder takes it at construction; the graph-first
        responder (assembled by ``agentic_search``, which this app must not edit)
        gets it via ``dataclasses.replace`` after it is returned. ``None`` leaves
        the responder's ``model_name`` untouched → the configured default.

        The whole graph-first probe is import-guarded + best-effort: ANY failure
        resolving the workspace degrades silently to the default path, so a
        graph-less install or a search-store hiccup never breaks ``/v1/structured``.
        """
        surface = request_surface()
        if workspace:
            responder = StructuredResource._graph_first_responder(
                schema, workspace, tools, surface
            )
            if responder is not None:
                # One seam, both paths: override the model the graph-first
                # responder was built with (a frozen dataclass → ``replace``).
                if model:
                    responder = dataclasses.replace(responder, model_name=model)
                return responder
        return StructuredResponder(
            runtime=_runtime,
            schema=schema,
            workspace=workspace,
            allowed_tools=tools,
            model_name=model,
            source_platform=surface,
        )

    @staticmethod
    def _graph_first_responder(
        schema: dict[str, Any],
        workspace: str,
        tools: list[str] | None,
        surface: str,
    ) -> StructuredResponder | None:
        """Return a graph-first responder iff *workspace* is an eligible SCG one.

        ``None`` ⇒ not a (mapped, enabled) search workspace; the caller falls
        back to the default grounding path. Import-guarded so a graph-less
        install simply never takes the graph-first branch.
        """
        try:
            from mewbo_api.agentic_search.scg.graph_structured_runner import (
                GraphStructuredRunner,
            )
            from mewbo_api.agentic_search.store import get_store
        except ImportError:
            return None
        try:
            runner = GraphStructuredRunner(store=get_store())
            ws = runner.workspace_for(workspace)
            if ws is None or not runner.is_graph_eligible(ws):
                return None
            return runner.build_responder(
                ws,
                runtime=_runtime,
                schema=schema,
                tools=tools,
                source_platform=surface,
            )
        except Exception as exc:  # noqa: BLE001 — fall back to default grounding
            logging.warning("graph-first structured resolution failed: {}", exc)
            return None

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

    @structured_ns.doc(
        description=(
            "Resolve a run handle returned by `POST /v1/structured` to its current "
            "state. While the run is in flight the body is `{run_id, status}`. Once "
            "a validated output exists, `status` is `completed` and `output` carries "
            "the schema-validated object; graph-grounded runs also carry a "
            "`provenance` object summarizing the pathways routed and probes "
            "executed. A run that ends without a valid output returns 422 with an "
            "error envelope. Poll this endpoint until `status` is `completed` (or a "
            "4xx)."
        ),
        params={
            "run_id": (
                "Run handle returned by POST /v1/structured, "
                "in the form <session_id>:r<seq>."
            )
        },
    )
    @structured_ns.response(
        200, "Run status, with output and provenance once completed", _run_status_model
    )
    @kit.errors(404, 422)
    @kit.errors(503, shape="message")
    @kit.auth_error()
    def get(self, run_id: str) -> tuple[dict, int]:
        """Get a structured run.

        Resolves a run handle to its current state. While the run is in flight the
        body is `{run_id, status}`. Once a validated output exists the status is
        `completed` and the output is attached; graph-grounded runs also carry a
        `provenance` object summarizing the pathways routed and probes executed.
        A run that ends without a valid output returns 422 with an error envelope.
        """
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
            # ONE transcript read per GET (output + provenance derive from it).
            events = _runtime.session_store.load_transcript(session_id)
            output = _structured_output_from(events)
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
            body: dict[str, Any] = {
                "run_id": run_id,
                "status": "completed",
                "output": output,
            }
            # Graph-first runs (#77) carry additive pathway/probe provenance: the
            # auditor sees graph consulted → probes executed → emit. Absent for a
            # plain/wiki run that fanned no probes.
            provenance = _provenance_from(events)
            if provenance is not None:
                body["provenance"] = provenance.model_dump()
            return body, 200

        if output is not None and _is_validation_error_payload(output):
            # The emit tool gave up after the reask cap — a structured failure.
            return {"run_id": run_id, "status": status, **_error(422, _NO_RESULT_REASON)[0]}, 422

        if status in _TERMINAL_STATUSES:
            # Terminal with no structured_output → the model never produced one.
            return {"run_id": run_id, "status": status, **_error(422, _NO_RESULT_REASON)[0]}, 422

        # Still running, no output yet.
        return {"run_id": run_id, "status": status}, 200
