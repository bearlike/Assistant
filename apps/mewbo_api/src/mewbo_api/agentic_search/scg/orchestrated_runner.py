"""OrchestratedSearchRunner — the real ``SearchRunner`` over a Mewbo session.

This is the orchestration-team half of the ``runner.py`` seam: where
:class:`~mewbo_api.agentic_search.runner.EchoSearchRunner` replays canned
fixtures, this runner drives a tool-scoped ``SessionRuntime`` session of the
``scg-search`` traversal agent and **translates its transcript into the same
normalized search-event protocol** (the ``events.py`` builders). The SCG is a
cheap router, not a parallel control loop — the one and only engine is the
existing ``ToolUseLoop`` the session already runs; this class adds no second
loop, it merely projects the session's transcript onto the run event log.

Parity with the echo runner (its sequence is the reference shape):

    run_started → (agent_start → agent_line* → agent_done)* → result*
                → answer_delta* → answer_ready → run_done | error

Synchronous semantics (mirrors :class:`EchoSearchRunner`): :meth:`start` drives
the session to completion via ``runtime.run_sync`` and appends a terminal event
(``run_done`` / ``error``) before returning — the run event log stays the single
authoritative status channel (no second channel; the SSE generator tails the
same log identically for echo and orchestrated runs).

Security invariants (spec §6 / subsystem CLAUDE.md):

* **``scg.enabled`` gate.** The whole feature ships behind ``scg.enabled``
  (default ``False``); when off, the run fails fast with a structured error
  rather than starting a session.
* **Untrusted workspace instructions.** ``workspace.instructions`` is untrusted
  prompt input — it is **never** concatenated into the system prompt /
  ``skill_instructions``. It is attached as an explicitly-labelled context event
  the agent may read via tools, never as a developer instruction.
* **No secrets.** Only the redacted query + tier + transcript projection are
  persisted; the runner never writes a token or credential to the run store.
* **Scoped tools.** ``allowed_tools`` is the run's path-capability grant
  (``RunRecord.allowed_tools``, already ``SourceCatalog.tools_for`` ∩
  ``filter_specs``) plus the fixed SCG traversal verbs — never the full catalog.
"""

from __future__ import annotations

from typing import Any, Literal

from mewbo_core.common import get_logger
from mewbo_core.permissions import auto_approve

from .. import events
from ..runner import _typewriter_chunks
from ..schemas import (
    AnswerSynthesis,
    RunPayload,
    RunRecord,
    SearchResult,
    TraceAgent,
    TraceLine,
    Workspace,
    utc_now_iso,
)
from .config import ScgConfig

logging = get_logger(name="api.agentic_search.scg.orchestrated_runner")

# The tier budget knob the ``scg-search`` agent reads (decomposition depth +
# probe fan-out). ``Auto`` is the default — a single loop, three knob settings,
# never three engines (spec §8 WITHDRAWN: no parallel proof-search engine).
SearchTier = Literal["Fast", "Auto", "Deep"]
_DEFAULT_TIER: SearchTier = "Auto"
_VALID_TIERS: frozenset[str] = frozenset({"Fast", "Auto", "Deep"})

# Traversal verbs the search agent always needs, independent of which connector
# tools a run's sources unlock. Unioned with the run's scoped connector grant.
_TRAVERSAL_TOOLS: tuple[str, ...] = (
    "scg_route",
    "scg_memory",
    "spawn_agent",
    "check_agents",
    "steer_agent",
)

# The capability-gated AgentDef this runner drives (see scg-search.md frontmatter
# ``requires-capabilities: [scg]``); advertised via the session context event.
_SEARCH_CAPABILITY = "scg"


class OrchestratedSearchRunner:
    """Synchronous ``SearchRunner`` backed by a real ``scg-search`` session.

    Dependency-light by design: the only collaborator is the ``SessionRuntime``
    passed through ``start(..., runtime=...)`` (so tests inject a fake runtime
    feeding a canned transcript — no LLM, no real session). State per run lives
    on the store's event log, not on the instance, so one runner is reusable.

    The default tier is :data:`_DEFAULT_TIER`; a per-run override may be supplied
    at construction (the route/façade picks it from the request).
    """

    def __init__(self, *, tier: SearchTier = _DEFAULT_TIER) -> None:
        """Bind the default search tier (budget knob) for runs this drives."""
        self.tier: SearchTier = tier if tier in _VALID_TIERS else _DEFAULT_TIER

    # -- SearchRunner Protocol ---------------------------------------------

    def start(
        self,
        run: RunRecord,
        workspace: Workspace,
        *,
        store: Any,
        runtime: Any = None,
    ) -> RunPayload:
        """Drive *run* via a real ``scg-search`` session; return the snapshot.

        Appends ``run_started`` immediately, then either (a) fails fast with an
        ``error`` terminal when the feature is disabled or no runtime is wired,
        or (b) starts the capability-scoped session, drives it to completion,
        translates the transcript into the normalized event sequence, and
        appends the terminal event. The returned :class:`RunPayload` is also
        persisted onto the record.
        """
        store.append_run_event(
            run.run_id,
            events.run_started(
                run_id=run.run_id,
                session_id=run.session_id,
                workspace_id=run.workspace_id,
                query=run.query,
                sources=list(workspace.sources),
            ),
        )

        if not ScgConfig.enabled():
            return self._fail(
                run,
                store=store,
                code="disabled",
                message="SCG search is disabled (scg.enabled is off).",
            )
        if runtime is None:
            return self._fail(
                run,
                store=store,
                code="no_runtime",
                message="No SessionRuntime wired for the orchestrated runner.",
            )

        try:
            session_id = self._drive_session(run, workspace, runtime=runtime)
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            logging.warning("scg-search run %s failed to drive: %s", run.run_id, exc)
            return self._fail(
                run, store=store, code="internal", message=str(exc)
            )

        records = runtime.load_events(session_id)
        return self._translate(run, store=store, session_id=session_id, records=records)

    # -- Session drive ------------------------------------------------------

    def _drive_session(
        self, run: RunRecord, workspace: Workspace, *, runtime: Any
    ) -> str:
        """Resolve + seed a capability-scoped session and run it to completion.

        Returns the resolved session id (patched onto the record). The query +
        tier seed the user turn; the untrusted workspace instructions are
        attached as a labelled context event ONLY — never the system prompt.
        """
        session_tag = f"agentic_search:scg:{run.run_id}"
        session_id = runtime.resolve_session(session_tag=session_tag)

        # Advertise the ``scg`` capability so spawn_agent can look up the
        # scg-search / scg-path-probe AgentDefs (gating mirrors wiki jobs.py).
        runtime.append_context_event(
            session_id, {"client_capabilities": [_SEARCH_CAPABILITY]}
        )

        # Untrusted prompt input — kept OUT of the system prompt. Attached as an
        # explicitly-labelled context event the agent may consult via tools.
        if workspace.instructions:
            runtime.append_context_event(
                session_id,
                {"untrusted_workspace_instructions": workspace.instructions},
            )

        allowed_tools = self._allowed_tools(run.allowed_tools)
        user_query = self._render_user_query(run.query, self.tier)

        runtime.run_sync(
            session_id=session_id,
            user_query=user_query,
            model_name=None,
            allowed_tools=allowed_tools,
            approval_callback=auto_approve,
        )
        return session_id

    @staticmethod
    def _allowed_tools(scoped: list[str]) -> list[str]:
        """Union the run's scoped connector grant with the SCG traversal verbs.

        ``scoped`` is the path-capability grant on the record (sources ∩
        ``filter_specs``); the traversal verbs are appended so the search agent
        can route + fan out. De-duplicated, selection order preserved.
        """
        seen: set[str] = set()
        out: list[str] = []
        for tool_id in (*scoped, *_TRAVERSAL_TOOLS):
            if tool_id not in seen:
                seen.add(tool_id)
                out.append(tool_id)
        return out

    @staticmethod
    def _render_user_query(query: str, tier: SearchTier) -> str:
        """Render the user turn carrying the query + tier knob for scg-search."""
        return f"query: {query}\ntier: {tier}\n\nProceed per the scg-search playbook."

    # -- Transcript → event protocol translation ---------------------------

    def _translate(
        self,
        run: RunRecord,
        *,
        store: Any,
        session_id: str,
        records: list[dict[str, Any]],
    ) -> RunPayload:
        """Project a finished session transcript onto the run event log.

        ``sub_agent`` lifecycle events become the per-pathway trace
        (``agent_start`` / ``agent_line`` / ``agent_done``); the final assistant
        answer becomes the ``answer_delta*`` typewriter + ``answer_ready``; the
        run's terminal state becomes ``run_done`` / ``error``. The accumulated
        :class:`RunPayload` is persisted onto the record.
        """
        trace = self._build_trace(records)
        for agent in trace:
            store.append_run_event(
                run.run_id,
                events.agent_start(
                    agent_id=agent.agent_id,
                    source_id=agent.source_id,
                    name=agent.name,
                    slot=agent.slot,
                ),
            )
            for line in agent.lines:
                store.append_run_event(
                    run.run_id, events.agent_line(agent_id=agent.agent_id, line=line)
                )
            # A probe that emitted any trace line did work — the console must not
            # grey it out as empty on a successful run. ``results_count`` stays 0
            # (probes synthesize into the answer, they don't emit result cards),
            # so ``empty`` reflects whether the lane produced output, not hits.
            store.append_run_event(
                run.run_id,
                events.agent_done(
                    agent_id=agent.agent_id,
                    results_count=0,
                    empty=not agent.lines,
                ),
            )

        # Results: the SCG search synthesizes a cited answer rather than emitting
        # per-source result cards (the connector return is the verifier, not a
        # normalized hit list); ``results`` stays empty until a probe contract
        # carries them. The trace + answer are the live surfaces today.
        results: list[SearchResult] = []

        status, answer_text, err = self._terminal(records)
        answer = AnswerSynthesis(tldr=answer_text, sources_count=len(results))

        if status == "completed":
            for chunk in _typewriter_chunks(answer_text):
                if chunk:
                    store.append_run_event(run.run_id, events.answer_delta(text=chunk))
            store.append_run_event(run.run_id, events.answer_ready(answer=answer))

        if status == "failed":
            return self._fail(
                run, store=store, code="agent_error", message=err or "run failed"
            )

        store.append_run_event(
            run.run_id, events.run_done(status=status, total_ms=0)
        )
        payload = RunPayload(
            run_id=run.run_id,
            session_id=session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status=status,
            total_ms=0,
            answer=answer if status == "completed" else AnswerSynthesis(),
            results=results,
            trace=trace,
        )
        store.update_run(
            run.run_id,
            session_id=session_id,
            status=status,
            completed_at=utc_now_iso(),
            payload=payload,
        )
        return payload

    @staticmethod
    def _build_trace(records: list[dict[str, Any]]) -> list[TraceAgent]:
        """Group ``sub_agent`` transcript events into per-pathway trace agents.

        One :class:`TraceAgent` per spawned probe (keyed on its ``agent_id``);
        every non-terminal lifecycle line becomes a :class:`TraceLine`, the
        ``stop`` action marks the final line ``done``. The agent's ``slot`` is
        its first-seen ordinal so the console can lay the lanes out stably.
        """
        agents: dict[str, TraceAgent] = {}
        order: list[str] = []
        for rec in records:
            if rec.get("type") != "sub_agent":
                continue
            payload = rec.get("payload") or {}
            agent_id = str(payload.get("agent_id") or "")
            if not agent_id:
                continue
            action = str(payload.get("action") or "")
            if agent_id not in agents:
                order.append(agent_id)
                agents[agent_id] = TraceAgent(
                    id=agent_id,
                    agent_id=agent_id,
                    name=str(payload.get("model") or "scg-path-probe"),
                    source_id=str(payload.get("parent_id") or ""),
                    slot=len(order) - 1,
                )
            detail = str(payload.get("detail") or action)
            agents[agent_id].lines.append(
                TraceLine(
                    t_ms=0,
                    glyph="✓" if action == "stop" else "·",
                    text=detail,
                    done=action == "stop",
                )
            )
        return [agents[a] for a in order]

    @staticmethod
    def _terminal(
        records: list[dict[str, Any]],
    ) -> tuple[Literal["completed", "failed", "cancelled"], str, str | None]:
        """Derive ``(status, answer_text, error)`` from the session transcript.

        Reads the last ``completion`` event: a ``done_reason`` of ``error`` /
        ``cancelled`` maps to that terminal; anything else is ``completed`` with
        the completion text as the synthesized answer. A transcript with no
        completion event (never ran) is treated as a failure.
        """
        completion: dict[str, Any] | None = None
        for rec in records:
            if rec.get("type") == "completion":
                completion = rec.get("payload") or {}
        if completion is None:
            return "failed", "", "no completion event in transcript"

        text = str(completion.get("text") or "")
        reason = str(completion.get("done_reason") or "")
        if reason in ("cancelled", "canceled"):
            return "cancelled", "", None
        if reason == "error" or completion.get("error"):
            return "failed", "", str(completion.get("error") or text or "run errored")
        return "completed", text, None

    # -- Failure terminal ---------------------------------------------------

    def _fail(
        self, run: RunRecord, *, store: Any, code: str, message: str
    ) -> RunPayload:
        """Append an ``error`` terminal + persist a failed snapshot; return it.

        The single failure path for every early-out and caught exception, so the
        event log always closes with exactly one terminal event (no second
        status channel).
        """
        store.append_run_event(run.run_id, events.error(code=code, message=message))
        payload = RunPayload(
            run_id=run.run_id,
            session_id=run.session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status="failed",
            error=message,
        )
        store.update_run(
            run.run_id,
            status="failed",
            completed_at=utc_now_iso(),
            error=message,
            payload=payload,
        )
        return payload


__all__ = ["OrchestratedSearchRunner", "SearchTier"]
