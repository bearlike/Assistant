#!/usr/bin/env python3
"""Schema-constrained structured responses: the ``emit_result`` SessionTool.

A caller supplies a JSON Schema for the desired output. We build an
``emit_result`` :class:`~mewbo_core.session_tools.SessionTool` whose function
parameters ARE that schema (bound verbatim via ``bind_tools``), run the normal
``ToolUseLoop`` so the model may call grounding tools, and terminate when the
model calls ``emit_result``. ``handle()`` validates ``tool_input`` against the
schema; on success it emits a ``structured_output`` event and signals
termination (the same ``ExitPlanModeTool`` pattern); on failure it feeds the
validation errors back as a tool-result so the model retries (bounded reask) —
reusing the existing tool-result feedback loop, so NO ``tool_choice`` plumbing
and NO loop change are needed.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import jsonschema

from mewbo_core.common import MockSpeaker, get_logger
from mewbo_core.llm import sanitize_tool_schema
from mewbo_core.permissions import auto_approve
from mewbo_core.prompt_registry import get_prompt_registry

# Force-emit directive — injected into the structured run's system prompt via
# the ``skill_instructions`` seam so the grounding model MUST conclude by
# calling ``emit_result`` (and never answers in prose). This fixes the failure
# where the model reaches natural completion without ever calling the emit tool
# → ``payload is None``. It lives in the prompt, NOT the loop, and needs no
# ``tool_choice`` plumbing. Sourced from the central registry
# (``structured.force_emit_directive``).
FORCE_EMIT_DIRECTIVE = get_prompt_registry().render("structured.force_emit_directive")

# Concise user query for the sharper belt-and-suspenders re-drive turn. The
# matching re-drive system directive (``structured.redrive_directive``) is
# rendered per-model at the call site (``_run_with_redrive``) so a per-model
# override reaches it too (#113); it still contains FORCE_EMIT_DIRECTIVE verbatim
# so tests/assertions that look for the base directive match either drive.
_REDRIVE_QUERY = get_prompt_registry().render("structured.redrive_query")

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.structured_response")

EMIT_RESULT_TOOL_ID = "emit_result"
STRUCTURED_OUTPUT_EVENT = "structured_output"
# Provenance tag PREFIX stamped on every agentic ``/v1/structured`` session so
# ``SessionOrigin``/``TraceProvenance`` classify it as ``structured`` instead of
# falling back to ``user`` (#78). The durable tag is ``structured:run:<session_id>``
# (unique per session) — a CONSTANT tag would collide on the tag-keyed store, so
# every run would overwrite one shared doc and all-but-the-latest run would
# silently lose its tag and reclassify to the ``user`` fallback (#87). The id
# segment is transparent to the parsers: ``SessionOrigin.classify`` prefix-matches
# ``structured:`` and ``_facets_from_tags`` reads the 2nd segment (``run``) as the
# ``session_type``. Per-run RESOLUTION is the storeless ``run_id``, not this tag.
STRUCTURED_RUN_TAG = "structured:run"
# Max schema-validation failures tolerated before giving up. The Nth failure
# terminates the run, so the model gets exactly N-1 reasks (2 with the default).
DEFAULT_MAX_FAILURES = 3

# Maps a workspace identifier to the capability a session must advertise so the
# grounding AgentDefs/tools (wiki graph, scg) appear. v1: any workspace grounds
# on the wiki capability; extend here when scg workspaces get a distinct slug.
_WORKSPACE_CAPABILITY = "wiki"


class _ResponderRuntime(Protocol):
    """The minimal ``SessionRuntime`` surface the responder composes.

    Typed as a Protocol so the responder stays a generic, DI-friendly core
    class (no import-up to an app's concrete runtime) while keeping mypy happy
    without an ``Any`` leak.
    """

    def resolve_session(
        self,
        *,
        session_id: str | None = ...,
        session_tag: str | None = ...,
    ) -> str: ...

    def tag_session(self, session_id: str, tag: str) -> None: ...

    def append_context_event(self, session_id: str, context: dict[str, object]) -> None: ...

    def append_event(self, session_id: str, event: dict[str, object]) -> None: ...

    def run_sync(self, **kwargs: object) -> object: ...

    def start_command(
        self, session_id: str, target: Callable[[threading.Event], None]
    ) -> bool: ...


class StructuredResponseError(RuntimeError):
    """Raised when a structured-response run produces no valid output."""


def build_emit_schema(schema: dict[str, object]) -> dict[str, object]:
    """Wrap *schema* into the OpenAI ``{"type":"function", ...}`` tool schema.

    The caller schema is the tool's ``parameters`` (sanitized for strict
    providers via :func:`sanitize_tool_schema`). Non-object roots are wrapped
    as ``{"result": <schema>}`` so the function parameters are always an object.
    """
    params = sanitize_tool_schema(schema)
    if not isinstance(params, dict) or params.get("type") != "object":
        params = {
            "type": "object",
            "properties": {"result": params},
            "required": ["result"],
        }
    return {
        "type": "function",
        "function": {
            "name": EMIT_RESULT_TOOL_ID,
            "description": (
                "Emit the final answer as a structured object. Call this exactly "
                "once when you have gathered enough grounding to populate every "
                "required field. The arguments you pass ARE the answer — they "
                "must validate against the provided schema."
            ),
            "parameters": params,
        },
    }


class EmitStructuredResponseTool:
    """``SessionTool`` that terminates a run with a schema-validated object.

    Built per session from the caller's JSON Schema. Satisfies the
    :class:`~mewbo_core.session_tools.SessionTool` Protocol via the class-shaped
    ``tool_id``/``schema``/``modes`` attributes and the ``handle`` /
    ``should_terminate_run`` methods.
    """

    tool_id: str = EMIT_RESULT_TOOL_ID
    modes: frozenset[str] = frozenset({"act"})

    def __init__(
        self,
        *,
        session_id: str,
        schema: dict[str, object],
        event_logger: Callable[[Event], None] | None = None,
        max_failures: int = DEFAULT_MAX_FAILURES,
    ) -> None:
        """Initialize the emit tool.

        Args:
            session_id: Session id (kept for parity with other SessionTools).
            schema: The caller's JSON Schema for the desired output object.
            event_logger: Callback for emitting the ``structured_output`` event.
            max_failures: Max validation failures tolerated before giving up.
                The Nth failure terminates the run, so the model gets N-1 reasks.
        """
        self._session_id = session_id
        self._raw_schema = schema
        self.schema: dict[str, object] = build_emit_schema(schema)
        # Validate against the *bound* parameters object (handles the
        # non-object-root wrap) so what the model sees == what we check.
        function = self.schema["function"]
        validate_schema = function["parameters"] if isinstance(function, dict) else schema
        assert isinstance(validate_schema, dict)  # noqa: S101 — build_emit_schema invariant
        self._validate_schema: dict[str, object] = validate_schema
        self._wrapped = not (isinstance(schema, dict) and schema.get("type") == "object")
        self._event_logger = event_logger
        self._max_failures = max_failures
        self._attempts = 0
        self._terminate_run_pending = False
        self.payload: object | None = None
        self.failed: bool = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        if self._terminate_run_pending:
            self._terminate_run_pending = False
            return True
        return False

    def terminal_reason(self) -> str:
        """A successful structured emit terminates with ``"completed"``, not approval."""
        return "completed"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Validate the tool input and either accept (terminate) or reask."""
        args = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            jsonschema.validate(instance=args, schema=self._validate_schema)
        except jsonschema.ValidationError as exc:
            return self._on_validation_error(exc)
        self.payload = args["result"] if self._wrapped else args
        self._emit({"type": STRUCTURED_OUTPUT_EVENT, "payload": self.payload})
        self._terminate_run_pending = True
        logging.info("Structured output accepted for session {}", self._session_id)
        return MockSpeaker(content="Structured output accepted. The run will now terminate.")

    def _on_validation_error(self, exc: jsonschema.ValidationError) -> MockSpeaker:
        """Reask under the cap; at the cap store a failure marker and terminate."""
        self._attempts += 1
        path = "/".join(str(p) for p in exc.absolute_path) or "<root>"
        detail = f"Field '{path}': {exc.message}"
        if self._attempts >= self._max_failures:
            self.failed = True
            self._emit(
                {
                    "type": STRUCTURED_OUTPUT_EVENT,
                    "payload": {"_error": "schema_validation_failed", "detail": detail},
                }
            )
            self._terminate_run_pending = True
            return MockSpeaker(
                content=get_prompt_registry().render(
                    "structured.reask_giveup",
                    attempts=self._attempts,
                    detail=detail,
                )
            )
        return MockSpeaker(
            content=get_prompt_registry().render(
                "structured.reask_fix_fields", detail=detail
            )
        )

    def _emit(self, event: Event) -> None:
        if self._event_logger is not None:
            try:
                self._event_logger(event)
            except Exception as exc:  # pragma: no cover - defensive
                logging.warning("Failed to emit structured_output event: {}", exc)


@dataclass(frozen=True)
class StructuredResponder:
    """Run one bounded agentic session that ends in a schema-validated object.

    Composes the proven one-shot blueprint (``runtime.run_sync`` with
    ``strict_tool_scope`` + ``auto_approve``) used by ``WikiQaSession`` and
    ``OrchestratedSearchRunner``, injecting the ``emit_result`` SessionTool so
    the model terminates by producing a validated object.
    """

    runtime: _ResponderRuntime
    schema: dict[str, object]
    workspace: str | None = None
    allowed_tools: list[str] | None = None
    model_name: str | None = None
    max_failures: int = DEFAULT_MAX_FAILURES
    session_tag: str | None = None
    source_platform: str | None = None
    # Graph-first extension seam (#77 — additive, app-injected, keeps core
    # graph-free). ``capabilities`` overrides the default ``wiki`` advertisement
    # (a search workspace advertises ``scg``); ``context_events`` are extra
    # context writes the binding supplies (e.g. quarantined workspace
    # instructions); ``extra_instructions`` are trusted skill_instructions
    # PREPENDED to the force-emit directive (the graph-first discipline
    # playbook); ``scope_factory`` is an injected context-manager factory bound
    # around each drive (the ``ScgScope`` source scope) so core never imports the
    # graph engine. All default to the historical behaviour.
    capabilities: list[str] | None = None
    context_events: list[dict[str, object]] = field(default_factory=list)
    extra_instructions: str | None = None
    scope_factory: Callable[[], AbstractContextManager[None]] | None = None

    def _prepare(self) -> tuple[str, EmitStructuredResponseTool]:
        """Resolve the session, scope its workspace, and build the emit tool.

        Shared by :meth:`run` and :meth:`start_async` so the schema-bound
        emit-tool construction + capability scoping live in exactly one place
        (DRY). Returns ``(session_id, emit)``; the same ``emit`` instance is the
        result holder for the sync path's re-drive.

        Provenance stamp (#78): an agentic structured run is tagged
        :data:`STRUCTURED_RUN_TAG` and its originating ``source_platform`` is
        written as a context event, so ``SessionOrigin``/``TraceProvenance``
        classify it as ``structured`` (not the ``user`` fallback) and the trace
        carries ``surface:<platform>``. Both signals are stamped BEFORE the run
        starts (the orchestrator reads them at run start). This single seam also
        covers the MCP ``structured_query`` tool, which posts to ``/v1/structured``.

        Graph-first (#77): when a search workspace is bound the caller supplies
        ``capabilities=["scg"]`` + the binding's ``context_events`` (capability
        advertisement + quarantined instructions). The default ``wiki``
        capability is used only when no explicit capabilities are given, so a
        wiki-grounded structured run is unchanged.
        """
        session_id = self.runtime.resolve_session(session_tag=self.session_tag)
        # Per-session tag (``structured:run:<id>``) — never the bare prefix, which
        # would collide on the tag-keyed store and let one run steal every other
        # run's tag (#87). The id segment is transparent to the prefix-matching
        # provenance parsers.
        self.runtime.tag_session(session_id, f"{STRUCTURED_RUN_TAG}:{session_id}")
        if self.source_platform:
            self.runtime.append_context_event(
                session_id, {"source_platform": self.source_platform}
            )
        for context in self.context_events:
            self.runtime.append_context_event(session_id, context)
        if self.workspace:
            if not self.context_events:
                # Default (wiki) grounding: advertise the wiki capability unless
                # the caller already supplied its own capability context events.
                self.runtime.append_context_event(
                    session_id,
                    {"client_capabilities": self.capabilities or [_WORKSPACE_CAPABILITY]},
                )
            self.runtime.append_context_event(
                session_id, {"structured_workspace": self.workspace}
            )
        emit = EmitStructuredResponseTool(
            session_id=session_id,
            schema=self.schema,
            max_failures=self.max_failures,
            # Persist the ``structured_output`` event to the transcript so the
            # ASYNC path works: ``GET /v1/structured/<run_id>`` reads the result
            # back via ``_load_structured_output``. Without this logger ``_emit``
            # no-ops, so a *successful* emit produces no event and the GET 422s
            # ("model did not emit") even though the run succeeded (#40). The
            # sync ``run()`` path read ``emit.payload`` in-memory and so masked
            # this; the MCP/REST async path could not.
            event_logger=lambda event: self.runtime.append_event(session_id, event),
        )
        return session_id, emit

    def _failure_reason(self, emit: EmitStructuredResponseTool) -> str | None:
        """Terminal failure reason, or None when the run produced a result.

        Same decision :meth:`run` makes — extracted so the sync raise and the
        async terminal-failure event share one source of truth (DRY).
        """
        if emit.failed:
            return f"Schema validation failed after {self.max_failures} attempts."
        if emit.payload is None:
            return "Run produced no structured output (the model never called emit_result)."
        return None

    def run(self, query: str) -> object:
        """Drive the session and return the validated structured object.

        The ``emit`` tool instance we inject IS the result holder: ``run_sync``
        terminates when the model calls ``emit_result``, at which point
        ``emit.payload`` / ``emit.failed`` are set the same way they would be in
        production. We read them directly — no transcript re-read.

        Forcing the emit is two-layered: every run carries
        :data:`FORCE_EMIT_DIRECTIVE` in its system prompt (via the
        ``skill_instructions`` seam), and — belt-and-suspenders — if the first
        run still produces no payload we do ONE bounded re-drive of the SAME
        session/emit tool with a sharper directive before giving up. No new
        control loop: the re-drive is just a second ``run_sync`` reusing the
        emit tool's existing reask machinery.
        """
        session_id, emit = self._prepare()
        self._run_with_redrive(session_id, query, emit)
        reason = self._failure_reason(emit)
        if reason is not None:
            raise StructuredResponseError(reason)
        return emit.payload

    def _run_with_redrive(
        self,
        session_id: str,
        query: str,
        emit: EmitStructuredResponseTool,
    ) -> None:
        """Drive one structured session, re-driving once if the model skips emit_result.

        Shared by :meth:`run` (sync) and :meth:`start_async` (in its background
        thread) so the belt-and-suspenders re-drive is DRY across both paths.
        Mutates *emit* in-place (sets ``payload``/``failed``); the caller reads
        those after this method returns.
        """
        self._drive(session_id, query, emit)
        if emit.payload is None and not emit.failed:
            # The model reached natural completion without calling emit_result.
            # Re-drive once with a sharper, mandatory directive — rendered for
            # this run's model so a per-model override applies (#113).
            redrive = get_prompt_registry().render(
                "structured.redrive_directive", model=self.model_name
            )
            self._drive(session_id, _REDRIVE_QUERY, emit, directive=redrive)

    def _drive(
        self,
        session_id: str,
        query: str,
        emit: EmitStructuredResponseTool,
        *,
        directive: str | None = None,
    ) -> None:
        """Run one bounded structured session that ends in an emit call.

        ``directive`` defaults to the force-emit directive rendered for this
        run's model so a per-model override of ``structured.force_emit_directive``
        applies (#113); the re-drive path passes the sharper directive explicitly.

        Graph-first (#77): ``extra_instructions`` (the graph-first discipline
        playbook) is PREPENDED to the force-emit directive — both ride the one
        trusted ``skill_instructions`` slot — and the drive runs inside the
        injected ``scope_factory`` (the ``ScgScope`` source scope) so the
        un-owned ``scg_route`` plugin tool only ranks pathways through the bound
        workspace's sources. Both default to no-ops, so the wiki path is
        unchanged.
        """
        if directive is None:
            directive = get_prompt_registry().render(
                "structured.force_emit_directive", model=self.model_name
            )
        skill = directive
        if self.extra_instructions:
            skill = f"{self.extra_instructions}\n\n{directive}"
        scope = self.scope_factory() if self.scope_factory else contextlib.nullcontext()
        with scope:
            self.runtime.run_sync(
                session_id=session_id,
                user_query=query,
                model_name=self.model_name,
                allowed_tools=self.allowed_tools,
                strict_tool_scope=True,
                approval_callback=auto_approve,
                extra_session_tools=[emit],
                skill_instructions=skill,
            )

    def start_async(self, query: str) -> str:
        """Kick the same schema-bound emit-tool session asynchronously.

        Runs the full drive (+ belt-and-suspenders re-drive) as ONE
        registry-managed background run via ``runtime.start_command`` — never a
        raw, untracked thread — so the run is serialized per session (a
        concurrent start is refused → ``""``), cancellable, and visible to
        ``is_running`` exactly like every other session run. (``SessionRuntime``
        is the only place a session runs — no parallel session implementations.)

        Returns a storeless per-run ``run_id`` of the form
        ``"<session_id>:r1"``: a structured run resolves a FRESH session per
        call (``resolve_session`` with no id/tag), so it is always that
        session's first and only run; ``""`` when the registry refuses (a run
        is already active). The ``structured_output`` transcript event is the
        authoritative result for callers polling ``GET /v1/structured/<run_id>``.
        """
        session_id, emit = self._prepare()

        def _target(_cancel: threading.Event) -> None:
            self._run_with_redrive(session_id, query, emit)
            reason = self._failure_reason(emit)
            if reason is not None:
                # No result → persist an authoritative terminal failure so
                # summarize_session reports `failed` (not silent `completed`)
                # and GET surfaces the reason — never a late phantom 422.
                self.runtime.append_event(
                    session_id,
                    {
                        "type": "completion",
                        "payload": {"done": False, "done_reason": "error", "reason": reason},
                    },
                )

        started = self.runtime.start_command(session_id, _target)
        return f"{session_id}:r1" if started else ""


__all__ = [
    "DEFAULT_MAX_FAILURES",
    "EMIT_RESULT_TOOL_ID",
    "FORCE_EMIT_DIRECTIVE",
    "STRUCTURED_OUTPUT_EVENT",
    "STRUCTURED_RUN_TAG",
    "EmitStructuredResponseTool",
    "StructuredResponder",
    "StructuredResponseError",
    "build_emit_schema",
]
