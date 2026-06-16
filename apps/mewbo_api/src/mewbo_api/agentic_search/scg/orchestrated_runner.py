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

Asynchronous semantics (mirrors :class:`MapSourceJob`): :meth:`start` appends
``run_started``, seeds the session, and launches the drive on the runtime's
managed background worker (``runtime.start_command`` — the same ``RunRegistry``
seam ``start_async`` rides, serialized per session and cancellable via
``should_cancel``), returning a ``running`` snapshot promptly. The worker
settles the run when the session ends — terminal status from
``runtime.summarize_session`` (the engine's single status chokepoint), terminal
event appended event-first — so the run event log stays the single
authoritative status channel (the SSE generator tails it; the MCP facade polls
the snapshot) and ``runtime.cancel(session_id)`` actually reaches a registered
``RunHandle`` (a bare ``run_sync`` never registers one, which made cancel a
no-op by construction and let a dead worker strand a ``running`` record).

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

import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from mewbo_core.common import get_logger
from mewbo_core.permissions import auto_approve
from mewbo_core.session_event_bus import get_session_event_bus

from .. import events
from ..runner import _typewriter_chunks
from ..schemas import (
    TERMINAL_RUN_STATUSES,
    AnswerSynthesis,
    RunPayload,
    RunRecord,
    RunStatsWire,
    SearchResult,
    TraceAgent,
    TraceLine,
    Workspace,
    utc_now_iso,
)
from .config import ScgConfig
from .playbooks import load_playbook
from .related_questions import RelatedQuestionsRunner
from .run_streamer import (
    _COORDINATOR_AGENT_ID,
    _COORDINATOR_NAME,
    CoordinatorTrace,
    ProbeTrace,
    ResultsProjection,
    RunEventStreamer,
)
from .workspace_binding import WorkspaceGraphBinding

logging = get_logger(name="api.agentic_search.scg.orchestrated_runner")

RunTerminalStatus = Literal["completed", "failed", "cancelled"]

# Kind label for the synthetic root-coordinator lane (#95) — distinct from the
# probe kind so the console can style the root-inline lane separately.
_COORDINATOR_KIND = "coordinator"

# Bound on the parallel follow-up call's join — the structured round-trip is
# ~1-3s, so this is a generous ceiling that lets it (almost) always finish while
# never letting a hung call strand the settle worker.
_RELATED_Q_TIMEOUT_S = 20.0


@dataclass
class _RelatedAsync:
    """Handle to the in-flight parallel follow-up call (thread + result box)."""

    thread: threading.Thread
    box: dict[str, list[str]]


@dataclass(frozen=True)
class _LaneStats:
    """Per-agent telemetry derived from ``llm_call_*`` events (settle-only)."""

    steps: int | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None


def _duration_ms(start_iso: str | None, end_iso: str | None) -> int | None:
    """Milliseconds between two ISO timestamps, or ``None`` when either is absent."""
    if not start_iso or not end_iso:
        return None
    try:
        start = datetime.fromisoformat(start_iso)
        end = datetime.fromisoformat(end_iso)
    except ValueError:
        return None
    return max(int((end - start).total_seconds() * 1000), 0)


# A search/map drive must NOT auto-inject the generic skill catalog — the
# trusted scg-* playbook is the ONLY system-prompt extension (untrusted
# source/workspace prose rides the user turn). The engine accepts
# ``run_sync(enable_skills=...)``, but test fakes may not, so include the
# kwarg ONLY when the runtime's ``run_sync`` accepts it (introspect the
# signature once). A **kwargs-accepting run_sync also passes.
def _skills_opt_out(runtime: Any) -> dict[str, Any]:
    """Return ``{"enable_skills": False}`` iff ``runtime.run_sync`` accepts it."""
    try:
        import inspect  # noqa: PLC0415

        sig = inspect.signature(runtime.run_sync)
    except (TypeError, ValueError):
        return {}
    params = sig.parameters
    accepts = "enable_skills" in params or any(
        p.kind == p.VAR_KEYWORD for p in params.values()
    )
    return {"enable_skills": False} if accepts else {}


class OrchestratedSearchRunner:
    """Async ``SearchRunner`` backed by a real ``scg-search`` session.

    Dependency-light by design: the only collaborator is the ``SessionRuntime``
    passed through ``start(..., runtime=...)`` (so tests inject a fake runtime
    feeding a canned transcript — no LLM, no real session). State per run lives
    on the store's event log + record, not on the instance — including the tier
    (the budget knob rides ``RunRecord.tier``, never the runner) — so one
    runner is reusable.

    The ONLY instance state is the optional :class:`RelatedQuestionsRunner` seam
    (the parallel follow-up generator). It is ``None`` by default — disabled, so
    a fake-runtime/echo test never spawns an LLM (the SCG no-real-LLM rule) and
    falls back to the agent-emitted transcript ``related_questions``. Production
    arms it in :func:`get_search_runner`; a test arms it with a stubbed
    synthesizer to exercise the parallel path.
    """

    def __init__(self, related_runner: RelatedQuestionsRunner | None = None) -> None:
        """Bind the optional follow-up generator (``None`` ⇒ the legacy path)."""
        self._related_runner = related_runner

    # -- SearchRunner Protocol ---------------------------------------------

    def start(
        self,
        run: RunRecord,
        workspace: Workspace,
        *,
        store: Any,
        runtime: Any = None,
        source_platform: str | None = None,
    ) -> RunPayload:
        """Launch *run* on the runtime's managed worker; return a running snapshot.

        Appends ``run_started`` immediately, then either (a) fails fast with an
        ``error`` terminal when the feature is disabled or no runtime is wired,
        or (b) seeds the capability-scoped session, patches the real session id
        onto the record (so ``POST /runs/<id>/cancel`` → ``runtime.cancel``
        reaches the registry handle), and starts the drive via
        ``runtime.start_command``. The worker appends every subsequent event and
        settles the terminal state — the returned payload is a ``running``
        snapshot, never the terminal one.
        """
        # Fail-fast paths carry no resolved session, so ``run_started`` uses the
        # tag placeholder (the only id available). The happy path resolves the
        # REAL session first, so ``run_started`` can carry the genuine session id
        # (the EVIDENCE: ``run_started.session_id`` carried the
        # ``agentic_search:run:<id>`` tag, not the real session id).
        def _emit_started(session_id: str) -> None:
            store.append_run_event(
                run.run_id,
                events.run_started(
                    run_id=run.run_id,
                    session_id=session_id,
                    workspace_id=run.workspace_id,
                    query=run.query,
                    sources=list(workspace.sources),
                ),
            )

        if not ScgConfig.enabled():
            _emit_started(run.session_id)
            return self._fail(
                run,
                store=store,
                code="disabled",
                message="SCG search is disabled (scg.enabled is off).",
            )
        if runtime is None:
            _emit_started(run.session_id)
            return self._fail(
                run,
                store=store,
                code="no_runtime",
                message="No SessionRuntime wired for the orchestrated runner.",
            )

        # The workspace binding seam (#77): the ONE place a workspace confers the
        # ``scg`` capability + graph traversal tools + the source scope. The same
        # seam the structured graph-first path reuses.
        binding = WorkspaceGraphBinding.for_workspace(workspace, run.allowed_tools)

        try:
            session_id = self._seed_session(
                run, binding, runtime=runtime, source_platform=source_platform
            )
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            _emit_started(run.session_id)
            logging.warning("scg-search run %s failed to seed: %s", run.run_id, exc)
            return self._fail(run, store=store, code="internal", message=str(exc))

        # The real session is resolved — emit ``run_started`` with the genuine id.
        _emit_started(session_id)

        # Patch the REAL session id before returning so the cancel route can
        # reach the registry handle while the worker drives. ALSO persist the
        # grant the binding ACTUALLY drives with (connector read-grant ∪ the
        # SCG traversal verbs) onto ``RunRecord.allowed_tools`` — the EVIDENCE:
        # the recorded grant (``SourceCatalog.tools_for`` alone) diverged from
        # what ``binding.allowed_tools()`` fed ``run_sync``, so the audit field
        # lied. One source of truth: record the actual grant.
        actual_grant = binding.allowed_tools()
        run = run.model_copy(
            update={"session_id": session_id, "allowed_tools": actual_grant}
        )
        store.update_run(
            run.run_id, session_id=session_id, allowed_tools=actual_grant
        )

        # Live projection (#77): subscribe to the backing session's event bus
        # BEFORE the drive so each probe's ``sub_agent`` event is projected onto
        # the run log AS it happens — the console reveals lanes live instead of
        # waiting for the whole run to finish. Reuses the SideStage SessionEventBus
        # seam, not a new transport.
        streamer = RunEventStreamer(
            run_id=run.run_id, store=store, bus=get_session_event_bus()
        )
        streamer.subscribe(session_id)

        def _drive(cancel_event: threading.Event) -> None:
            """Run the session to completion on the worker; settle the run."""
            try:
                streamer.start()
                with binding.scope():
                    runtime.run_sync(
                        session_id=session_id,
                        user_query=self._render_user_query(run.query, run.tier),
                        # An explicit per-run override wins; else the tier
                        # picks the brain (fast→nano / auto→sonnet / deep→
                        # frontier via scg.traversal.tier_models); None
                        # (blank/unknown) falls back to llm.default_model.
                        # Probes inherit the session model.
                        model_name=run.model or ScgConfig.model_for_tier(run.tier),
                        allowed_tools=binding.allowed_tools(),
                        skill_instructions=load_playbook("scg-search"),
                        approval_callback=auto_approve,
                        should_cancel=cancel_event.is_set,
                        # The scg-search playbook is the ONLY trusted system-prompt
                        # extension — opt out of generic skill auto-injection.
                        **_skills_opt_out(runtime),
                    )
                streamer.stop()
                records = runtime.load_events(session_id)
                summary = runtime.summarize_session(session_id)
                self._settle(
                    run,
                    store=store,
                    session_id=session_id,
                    records=records,
                    summary=summary,
                    streamer=streamer,
                )
            except Exception as exc:  # noqa: BLE001 — settle as structured failure
                streamer.stop()
                logging.warning(
                    "scg-search run %s failed to drive: %s", run.run_id, exc
                )
                self._fail(run, store=store, code="internal", message=str(exc))

        if not runtime.start_command(session_id, _drive):
            # The registry refused (a run is already active on the session).
            return self._fail(
                run,
                store=store,
                code="busy",
                message="session already has an active run",
            )

        return RunPayload(
            run_id=run.run_id,
            session_id=session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status="running",
            tier=run.tier,
            model=run.model,
        )

    # -- Session seeding ------------------------------------------------------

    def _seed_session(
        self,
        run: RunRecord,
        binding: WorkspaceGraphBinding,
        *,
        runtime: Any,
        source_platform: str | None = None,
    ) -> str:
        """Resolve + seed a capability-scoped session; return its id.

        The scg-search playbook is the trusted ``skill_instructions`` extension
        (passed at drive time); the capability advertisement + the untrusted
        workspace instructions ride the binding's context events — the latter
        as a labelled context event ONLY, never the system prompt.

        The session tag is ``agentic_search:run:<run_id>`` so ``TraceProvenance``
        classifies it ``search`` / ``session_type=search_run`` — NOT the
        ``scg_map`` mislabel the old ``agentic_search:scg:`` tag produced (a
        search RUN is not a map; ``scg:map:`` is the mapper's own tag). #77.

        ``source_platform`` (when the route forwards it) is stamped as the
        session's surface context event so the Langfuse trace reads
        ``surface:<platform>`` instead of ``surface:unknown`` (#77).
        """
        session_tag = f"agentic_search:run:{run.run_id}"
        session_id = runtime.resolve_session(session_tag=session_tag)

        # Capability advertisement + quarantined untrusted instructions — the
        # ONE seam (#77). Advertising ``scg`` lets spawn_agent look up the
        # scg-search / scg-path-probe AgentDefs (gating mirrors wiki jobs.py).
        for context in binding.context_events:
            runtime.append_context_event(session_id, context)
        if source_platform:
            runtime.append_context_event(
                session_id, {"source_platform": source_platform}
            )
        return session_id

    @staticmethod
    def _render_user_query(query: str, tier: str) -> str:
        """Render the user turn carrying the query + tier knob for scg-search.

        The wire tier is lowercase (``fast|auto|deep``); the playbook's knob
        vocabulary is capitalized (``Fast | Auto | Deep``), so capitalize here.
        """
        return (
            f"query: {query}\ntier: {tier.capitalize()}\n\n"
            "Proceed per the scg-search playbook."
        )

    # -- Transcript → event protocol settle ---------------------------------

    def _settle(
        self,
        run: RunRecord,
        *,
        store: Any,
        session_id: str,
        records: list[dict[str, Any]],
        summary: dict[str, Any],
        streamer: RunEventStreamer | None = None,
    ) -> RunPayload | None:
        """Reconcile a finished session transcript onto the run event log.

        The worker's one terminal path (event first, snapshot second — the
        ``MapSourceJob._settle`` stance). The per-pathway trace
        (``agent_start`` / ``agent_line`` / ``agent_done``) is now streamed LIVE
        by :class:`RunEventStreamer` as each probe runs; settle only
        RECONCILES — :meth:`RunEventStreamer.reconcile_missing` flushes any agent
        the live stream did not already emit (a fast run whose ``completion``
        landed before the consumer drained, a bus drop, or a fake-runtime test
        with no live bus). The final assistant answer becomes the
        ``answer_delta*`` typewriter + ``answer_ready``; the terminal status
        comes from *summary* (see :meth:`_run_status`). A record the cancel
        route already settled is left untouched — never a second terminal event.
        """
        if self._already_settled(store, run.run_id):
            return None

        trace = self._build_trace(records)
        # Probe lanes are the classification key for tool_result events (#102):
        # a child loop inherits the parent's event_logger, so probe tool calls
        # land on THIS transcript stamped with the probe's agent_id — they must
        # never read as root/coordinator activity. Same classifier as the live
        # streamer (agent_id ∈ probe lanes), so live and settle agree.
        probe_ids = {agent.agent_id for agent in trace}
        # The root coordinator's own tool activity (#95): a root-inline run (fast
        # tier, no probe sub-agents) streams nothing through the probe lanes, so
        # the root's ``tool_result`` events are projected as one extra lane. Its
        # slot is its first-seen ordinal across the MERGED stream (probes +
        # coordinator) so the settle reconcile honours transcript order exactly as
        # the live path does (coordinator opens when its first tool_result lands).
        # This re-slots the probe lanes in place to leave room for it.
        coordinator_slot = self._assign_lane_slots(records, trace, probe_ids)
        coordinator_lines = self._build_coordinator_lines(records, probe_ids)
        # ``scg_results`` emits the discrete result cards (transcript-as-transport):
        # the root emits once before synthesis; each probe may emit its own cards
        # (#102). The api projects every emit, ids salted by the emitting probe.
        results, emitter_counts, returned_counts = self._build_results(
            run.run_id, records, probe_ids
        )
        # Credit each probe lane its TRUE kept count + its raw returned count (the
        # old hardcoded 0 was blind to a probe that emitted 3 cards; the
        # returned − kept delta is the lane's "N filtered").
        for agent in trace:
            agent.results_count = emitter_counts.get(agent.agent_id, 0)
            agent.returned_count = returned_counts.get(agent.agent_id, 0)
        coordinator_count = emitter_counts.get(_COORDINATOR_AGENT_ID, 0)
        coordinator_returned = returned_counts.get(_COORDINATOR_AGENT_ID, 0)
        # A run is data-bearing iff any probe returned data OR any result emitted.
        with_data_probes = [a for a in trace if not ProbeTrace.is_dead_end(a.result)]
        has_data = bool(with_data_probes) or bool(results)

        if streamer is not None:
            streamer.reconcile_missing(trace)
            streamer.reconcile_results(results)
            if coordinator_lines or streamer.coordinator_opened():
                streamer.reconcile_coordinator(
                    coordinator_lines,
                    slot=coordinator_slot,
                    has_data=has_data,
                    results_count=coordinator_count,
                    returned_count=coordinator_returned,
                )
        else:
            # No live streamer (defensive / legacy call): emit the full trace.
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
                store.append_run_event(
                    run.run_id,
                    events.agent_done(
                        agent_id=agent.agent_id,
                        results_count=agent.results_count,
                        returned_count=agent.returned_count,
                        empty=not agent.lines,
                    ),
                )
            for item in results:
                store.append_run_event(run.run_id, events.result(item=item))

        status = self._run_status(summary)
        answer_text, err = self._task_result(records)
        # Confidence + sources_count come from REAL probe + result signals, not a
        # fixture (the echo-era fields read 0% / 0 sources on real runs). Probes
        # AND emitted results both ground the answer (a root-inline run has only
        # results); the coordinator lane is NOT a probe — never in the ratio.
        confidence, sources_count = self._synthesis_metrics(trace, results)
        answer = AnswerSynthesis(
            tldr=answer_text, confidence=confidence, sources_count=sources_count
        )

        if status == "failed":
            return self._fail(
                run,
                store=store,
                code="agent_error",
                message=err
                or f"run ended: {summary.get('done_reason') or 'unknown'}",
            )

        # Kick off the follow-up suggestions in PARALLEL with the answer reveal —
        # its own structured LLM call, OFF the main answer path (a follow-up list
        # is never load-bearing). Started BEFORE the answer events so they stream
        # without waiting on it; joined below, before the terminal run_done.
        related_async = self._start_related_questions(run, answer_text, status)

        if status == "completed":
            for chunk in _typewriter_chunks(answer_text):
                if chunk:
                    store.append_run_event(run.run_id, events.answer_delta(text=chunk))
            store.append_run_event(run.run_id, events.answer_ready(answer=answer))

        # Honest elapsed: started_at/created_at ISO → settle, in ms (#95). The
        # old hardcoded 0 read ``0ms`` next to a ~3-minute run. The terminal
        # ``run_done`` is emitted below, AFTER the related-questions event, so the
        # stream (which closes on run_done) still delivers the follow-ups.
        total_ms = self._elapsed_ms(run)
        # The persisted trace ALSO carries the synthetic coordinator lane (#95)
        # so a zero-probe run's snapshot stops showing ``trace:[]`` beside a real
        # answer. The coordinator's ``result`` stays "" (its synthesis is the
        # answer, never duplicated into a lane response).
        persisted_trace = list(trace)
        coordinator_agent = self._build_coordinator_agent(
            records,
            coordinator_lines,
            slot=coordinator_slot,
            results_count=coordinator_count,
            returned_count=coordinator_returned,
            has_data=has_data,
        )
        if coordinator_agent is not None:
            persisted_trace.append(coordinator_agent)
        # Honest derived stats — NEVER fabricated. Underivable ms stays None.
        stats = self._build_stats(
            run, records, trace, results, total_ms=total_ms
        )
        # Resolve the parallel follow-up call (bounded join), falling back to the
        # agent-emitted transcript value, then the live capture. Emit it BEFORE the
        # terminal run_done so the stream still delivers it (the snapshot carries
        # it on the payload regardless).
        related = self._resolve_related_questions(related_async, records, streamer)
        if related:
            store.append_run_event(
                run.run_id, events.related_questions(questions=related)
            )
        store.append_run_event(
            run.run_id, events.run_done(status=status, total_ms=total_ms)
        )
        payload = RunPayload(
            run_id=run.run_id,
            session_id=session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status=status,
            tier=run.tier,
            model=run.model,
            total_ms=total_ms,
            answer=answer if status == "completed" else AnswerSynthesis(),
            results=results,
            trace=persisted_trace,
            related_questions=related,
            stats=stats,
        )
        store.update_run(
            run.run_id,
            session_id=session_id,
            status=status,
            completed_at=utc_now_iso(),
            total_ms=total_ms,
            payload=payload,
        )
        store.update_past_query(
            run.workspace_id, run.run_id, status=status, results=len(results)
        )
        return payload

    @staticmethod
    def _already_settled(store: Any, run_id: str) -> bool:
        """True when the record is already terminal (e.g. the cancel route won)."""
        record = store.get_run(run_id)
        return record is not None and record.status in TERMINAL_RUN_STATUSES

    @staticmethod
    def _run_status(summary: dict[str, Any]) -> RunTerminalStatus:
        """Project ``summarize_session``'s vocabulary onto the run statuses.

        ``summarize_session`` is the engine's single status chokepoint — never
        re-derive status from the raw completion payload (that drift shipped
        two bugs: non-success ``done_reason`` values coerced to ``completed``,
        and a guard on a ``"cancelled"`` spelling the engine never emits — the
        loop says ``"canceled"``). Mapping: ``completed`` → ``completed``;
        ``canceled`` → ``cancelled``; every other summary status (``failed`` /
        ``incomplete`` / ``awaiting_approval`` / ``idle``) is a non-success
        terminal → ``failed``.

        One wrinkle: the settle executes INSIDE the worker ``start_command``
        registered, so the summary's ``is_running`` override reports our own
        still-alive drive thread as ``running``. The turn itself is finished
        (``run_sync`` returned), so fall back to the ``done_reason`` the
        summary forwards verbatim — same chokepoint, minus the
        self-observation override; ``completed``/``canceled`` are the only
        success/cancel reasons the loop emits.
        """
        status = str(summary.get("status") or "")
        if status == "running":
            reason = str(summary.get("done_reason") or "")
            if reason == "canceled":
                return "cancelled"
            return "completed" if reason == "completed" else "failed"
        if status == "completed":
            return "completed"
        if status == "canceled":
            return "cancelled"
        return "failed"

    @staticmethod
    def _build_trace(records: list[dict[str, Any]]) -> list[TraceAgent]:
        """Group ``sub_agent`` transcript events into per-pathway trace agents.

        One :class:`TraceAgent` per spawned probe (keyed on its ``agent_id``);
        every non-terminal lifecycle line becomes a :class:`TraceLine`, the
        ``stop`` action marks the final line ``done``. The agent's ``slot`` is
        its first-seen ordinal so the console can lay the lanes out stably.

        Each lane is enriched with honest provenance: ``kind`` (the agent KIND,
        e.g. ``scg-path-probe``), ``model`` (the LLM it ran on), and the
        per-lane telemetry (``steps`` / ``duration_ms`` / ``input_tokens`` /
        ``output_tokens``) derived from this transcript's ``llm_call_*`` events +
        the ``stop`` aggregates — all ``None`` when underivable.
        """
        lane_stats = OrchestratedSearchRunner._lane_stats(records)
        agents: dict[str, TraceAgent] = {}
        order: list[str] = []
        for rec in records:
            if rec.get("type") != "sub_agent":
                continue
            payload = rec.get("payload") or {}
            agent_id = str(payload.get("agent_id") or "")
            if not agent_id:
                continue
            if agent_id not in agents:
                order.append(agent_id)
                agents[agent_id] = TraceAgent(
                    id=agent_id,
                    agent_id=agent_id,
                    name=ProbeTrace.lane_name(payload),
                    source_id=ProbeTrace.source_id(payload),
                    slot=len(order) - 1,
                    kind=ProbeTrace.lane_name(payload),
                    model=ProbeTrace.model(payload),
                )
            # ONE projection shared with the live streamer (DRY) so a settle-time
            # reconciled lane is byte-identical to one that streamed live.
            agents[agent_id].lines.append(ProbeTrace.line(payload))
            # The terminal ``stop`` carries the probe's compressed evidence block
            # (``summary``) — capture it for the lane's response panel + metrics.
            summary = ProbeTrace.result_text(payload)
            if summary:
                agents[agent_id].result = summary
            # The ``stop`` aggregates carry the loop's own step/token tallies.
            if str(payload.get("action") or "") == "stop":
                OrchestratedSearchRunner._apply_stop_aggregates(
                    agents[agent_id], payload
                )
        # Fold the per-lane llm_call-derived telemetry (duration; token/step
        # fallback when the stop aggregates were blank).
        for agent_id, agent in agents.items():
            stats = lane_stats.get(agent_id)
            if stats is not None:
                OrchestratedSearchRunner._merge_lane_stats(agent, stats)
        return [agents[a] for a in order]

    @staticmethod
    def _apply_stop_aggregates(agent: TraceAgent, payload: dict[str, Any]) -> None:
        """Fold a ``sub_agent`` stop's ``steps_completed`` / token totals onto a lane.

        The hypervisor stamps the child's loop tallies on the terminal ``stop``
        event (``steps_completed`` / ``input_tokens`` / ``output_tokens``) — the
        authoritative per-lane numbers when present (>0). Left ``None`` for a
        zero/absent value so the llm_call fallback can fill it.
        """
        steps = payload.get("steps_completed")
        if isinstance(steps, int) and steps > 0:
            agent.steps = steps
        in_tok = payload.get("input_tokens")
        if isinstance(in_tok, int) and in_tok > 0:
            agent.input_tokens = in_tok
        out_tok = payload.get("output_tokens")
        if isinstance(out_tok, int) and out_tok > 0:
            agent.output_tokens = out_tok

    @staticmethod
    def _merge_lane_stats(agent: TraceAgent, stats: _LaneStats) -> None:
        """Fill a lane's still-blank telemetry from its ``llm_call_*`` aggregate.

        ``duration_ms`` always comes from the llm_call span (the stop event has
        no duration); ``steps`` / tokens are filled ONLY when the stop
        aggregates left them blank — the stop tally wins when both exist.
        """
        if stats.duration_ms is not None:
            agent.duration_ms = stats.duration_ms
        if agent.steps is None and stats.steps:
            agent.steps = stats.steps
        if agent.input_tokens is None and stats.input_tokens:
            agent.input_tokens = stats.input_tokens
        if agent.output_tokens is None and stats.output_tokens:
            agent.output_tokens = stats.output_tokens

    @staticmethod
    def _lane_stats(records: list[dict[str, Any]]) -> dict[str, _LaneStats]:
        """Derive per-agent ``llm_call`` telemetry keyed by ``agent_id``.

        Scans ``llm_call_start`` / ``llm_call_end`` events (each stamped with the
        emitting ``agent_id`` + ``ts``): ``steps`` = the call count;
        ``duration_ms`` = last end ts − first start ts; tokens prefer the
        cumulative totals on the LAST end event, else the per-call sums.
        Everything is ``None``/0 when the transcript carries no llm_call events
        (a fake-runtime test) — the caller folds that as "underivable".
        """
        starts: dict[str, str] = {}
        ends: dict[str, str] = {}
        counts: dict[str, int] = {}
        sum_in: dict[str, int] = {}
        sum_out: dict[str, int] = {}
        cum_in: dict[str, int] = {}
        cum_out: dict[str, int] = {}
        for rec in records:
            kind = rec.get("type")
            if kind not in ("llm_call_start", "llm_call_end"):
                continue
            payload = rec.get("payload") or {}
            agent_id = str(payload.get("agent_id") or "")
            if not agent_id:
                continue
            ts = str(rec.get("ts") or payload.get("ts") or "")
            if kind == "llm_call_start":
                starts.setdefault(agent_id, ts)
            else:
                ends[agent_id] = ts  # last end wins
                counts[agent_id] = counts.get(agent_id, 0) + 1
                sum_in[agent_id] = sum_in.get(agent_id, 0) + int(
                    payload.get("input_tokens") or 0
                )
                sum_out[agent_id] = sum_out.get(agent_id, 0) + int(
                    payload.get("output_tokens") or 0
                )
                cum_in[agent_id] = int(payload.get("cumulative_input_tokens") or 0)
                cum_out[agent_id] = int(payload.get("cumulative_output_tokens") or 0)
        out: dict[str, _LaneStats] = {}
        agent_ids = set(starts) | set(ends)
        for agent_id in agent_ids:
            out[agent_id] = _LaneStats(
                steps=counts.get(agent_id, 0) or None,
                duration_ms=_duration_ms(
                    starts.get(agent_id), ends.get(agent_id)
                ),
                input_tokens=(cum_in.get(agent_id) or sum_in.get(agent_id) or 0)
                or None,
                output_tokens=(cum_out.get(agent_id) or sum_out.get(agent_id) or 0)
                or None,
            )
        return out

    @staticmethod
    def _build_coordinator_agent(
        records: list[dict[str, Any]],
        lines: list[TraceLine],
        *,
        slot: int | None,
        results_count: int,
        returned_count: int = 0,
        has_data: bool,
    ) -> TraceAgent | None:
        """Build the synthetic root-coordinator :class:`TraceAgent` for the snapshot.

        ``None`` when the root issued no tool calls (no coordinator lane — the
        slot is ``None``). The lane carries kind ``coordinator``, its digest
        ``lines``, the root's llm_call telemetry (the root agent_id is the lane's
        REAL agent_id in the transcript), and its OWN card count — but a BLANK
        ``result`` (the synthesis is the answer; never duplicated into a lane
        response). ``empty`` mirrors the live ``reconcile_coordinator``: false
        iff the run produced data.
        """
        if slot is None:
            return None
        root_id = OrchestratedSearchRunner._root_agent_id(records)
        stats = (
            OrchestratedSearchRunner._lane_stats(records).get(root_id or "")
            if root_id
            else None
        )
        agent = TraceAgent(
            id=_COORDINATOR_AGENT_ID,
            agent_id=_COORDINATOR_AGENT_ID,
            name=_COORDINATOR_NAME,
            source_id="",
            slot=slot,
            lines=list(lines),
            result="",
            kind=_COORDINATOR_KIND,
            results_count=results_count,
            returned_count=returned_count,
        )
        if stats is not None:
            agent.model = OrchestratedSearchRunner._root_model(records, root_id)
            OrchestratedSearchRunner._merge_lane_stats(agent, stats)
        return agent

    @staticmethod
    def _root_agent_id(records: list[dict[str, Any]]) -> str | None:
        """The root agent's id — the ``agent_id`` on a depth-0 ``llm_call``/tool event.

        The root coordinator's telemetry rides its real ``agent_id`` (not the
        ``coordinator`` sentinel). Prefer an event with ``depth == 0``; fall back
        to the first ``tool_result`` agent_id that never opened a probe lane.
        """
        for rec in records:
            payload = rec.get("payload") or {}
            if rec.get("type") in ("llm_call_start", "llm_call_end"):
                if payload.get("depth") == 0:
                    agent_id = str(payload.get("agent_id") or "")
                    if agent_id:
                        return agent_id
        return None

    @staticmethod
    def _root_model(records: list[dict[str, Any]], root_id: str | None) -> str | None:
        """The model the root coordinator ran on (from its ``tool_result``/llm_call)."""
        if not root_id:
            return None
        for rec in records:
            payload = rec.get("payload") or {}
            if str(payload.get("agent_id") or "") == root_id:
                model = payload.get("model")
                if model:
                    return str(model)
        return None

    @staticmethod
    def _build_stats(
        run: RunRecord,
        records: list[dict[str, Any]],
        trace: list[TraceAgent],
        results: list[SearchResult],
        *,
        total_ms: int,
    ) -> RunStatsWire:
        """Derive the honest run-stats block — NEVER fabricate an underivable value.

        ``probes`` = spawned probe lanes; ``tool_calls`` = every ``tool_result``
        event; tokens = the cross-lane totals (cumulative from the LAST
        ``llm_call_end`` per agent, else the per-call sums). ``setup_ms`` = the
        run's ``created_at`` → the first user/llm event (the pre-turn MCP
        handshake gap the "73s total" hid) — ``None`` when no such event has a
        timestamp; ``search_ms`` = ``total_ms − setup_ms`` (also ``None`` when
        ``setup_ms`` is). The RunStats discipline: a value that can't be derived
        stays ``None``, never a misleading 0.
        """
        lane_stats = OrchestratedSearchRunner._lane_stats(records)
        tool_calls = sum(1 for r in records if r.get("type") == "tool_result")
        input_tokens = sum(
            s.input_tokens or 0 for s in lane_stats.values()
        )
        output_tokens = sum(
            s.output_tokens or 0 for s in lane_stats.values()
        )
        setup_ms = OrchestratedSearchRunner._setup_ms(run, records)
        search_ms = (
            max(total_ms - setup_ms, 0) if setup_ms is not None else None
        )
        return RunStatsWire(
            probes=len(trace),
            tool_calls=tool_calls,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            setup_ms=setup_ms,
            search_ms=search_ms,
        )

    @staticmethod
    def _setup_ms(run: RunRecord, records: list[dict[str, Any]]) -> int | None:
        """Pre-turn wall clock: ``created_at`` → first user/llm event, in ms.

        ``None`` when the run has no parseable start stamp OR no user/llm event
        carries a timestamp (a fake-runtime transcript) — never a fabricated 0,
        so the console suppresses the chip rather than imply an instant setup.
        """
        start_iso = run.created_at or run.started_at
        if not start_iso:
            return None
        first_ts: str | None = None
        for rec in records:
            if rec.get("type") in ("user", "llm_call_start"):
                ts = str(rec.get("ts") or (rec.get("payload") or {}).get("ts") or "")
                if ts:
                    first_ts = ts
                    break
        if first_ts is None:
            return None
        return _duration_ms(start_iso, first_ts)

    @staticmethod
    def _assign_lane_slots(
        records: list[dict[str, Any]],
        trace: list[TraceAgent],
        probe_ids: set[str],
    ) -> int | None:
        """Re-slot probe lanes by MERGED first-seen order; return coordinator slot.

        The live streamer assigns a lane's slot from ``len(self._order)`` at its
        first event, so probe and coordinator slots interleave by transcript
        arrival. The settle path must reproduce that exact ordering. This scans
        the transcript once, assigning each lane (probe ``agent_id`` or the
        sentinel coordinator, opened by its first ROOT ``tool_result`` — a
        probe's own tool_result, classified by *probe_ids*, never opens it) its
        first-seen ordinal, then writes the probe ordinals back onto *trace* in
        place. Returns the coordinator's ordinal, or ``None`` when the root
        issued no tool_result (no coordinator lane).
        """
        slots: dict[str, int] = {}
        coordinator_key = "__coordinator__"
        for rec in records:
            kind = rec.get("type")
            payload = rec.get("payload") or {}
            if kind == "sub_agent":
                key = str(payload.get("agent_id") or "")
                if not key:
                    continue
            elif kind == "tool_result":
                if not CoordinatorTrace.is_root_event(payload):
                    continue
                if str(payload.get("agent_id") or "") in probe_ids:
                    continue
                key = coordinator_key
            else:
                continue
            if key not in slots:
                slots[key] = len(slots)
        for agent in trace:
            if agent.agent_id in slots:
                agent.slot = slots[agent.agent_id]
        return slots.get(coordinator_key)

    @staticmethod
    def _build_coordinator_lines(
        records: list[dict[str, Any]], probe_ids: set[str]
    ) -> list[TraceLine]:
        """Project the ROOT's ``tool_result`` events into coordinator trace lines.

        A ``tool_result`` in this transcript is NOT always the root's: probe
        tool calls ride the same transcript stamped with the probe's
        ``agent_id`` (the child loop inherits the parent's event_logger — #102
        corrected the #95 own-sessions premise), so any payload whose
        ``agent_id`` is a probe lane is excluded here exactly as the live
        streamer excludes it. Each remaining event becomes one secret-free
        :class:`CoordinatorTrace` digest line (the SAME projection the live
        streamer uses — DRY), so a reconciled coordinator lane is byte-identical
        to one that streamed live. Empty when the root issued no tool calls.
        """
        lines: list[TraceLine] = []
        for rec in records:
            if rec.get("type") != "tool_result":
                continue
            payload = rec.get("payload") or {}
            if str(payload.get("agent_id") or "") in probe_ids:
                continue
            if CoordinatorTrace.is_root_event(payload):
                lines.append(CoordinatorTrace.line(payload))
        return lines

    @staticmethod
    def _build_results(
        run_id: str, records: list[dict[str, Any]], probe_ids: set[str]
    ) -> tuple[list[SearchResult], dict[str, int], dict[str, int]]:
        """Collect the run's result cards + per-emitter kept & returned counts.

        Transcript-as-transport (#95/#102): the root emits once before the
        synthesis; each probe may emit its own grounded cards. The api projects
        every entry onto a stable-id :class:`SearchResult` via
        :class:`ResultsProjection` (the SAME read the live stream uses, salted
        by the emitting probe's ``agent_id``, so ids — and thus dedup — agree).

        Dedup is BOTH the stable id AND the cross-emitter SEMANTIC key
        (normalized url, else title+source) — FIRST emission wins — so the
        root's url-less re-emit of a probe's card never doubles it (the
        EVIDENCE: 5 cards for 3 results). Returns ``(results, kept_counts,
        returned_counts)`` keyed by emitter (the root coordinator by its
        sentinel id): ``kept`` is each lane's unique-card breadth (its
        ``results_count``); ``returned`` is its RAW emit count, so the
        ``returned − kept`` delta is the lane's "N filtered" (what collapsed
        into another lane's card).
        """
        out: list[SearchResult] = []
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        counts: dict[str, int] = {}
        returned: dict[str, int] = {}
        for rec in records:
            if rec.get("type") != "tool_result":
                continue
            payload = rec.get("payload") or {}
            if not ResultsProjection.is_results_event(payload):
                continue
            agent_id = str(payload.get("agent_id") or "")
            is_probe = agent_id in probe_ids
            emitter = agent_id if is_probe else None
            credit_to = agent_id if is_probe else _COORDINATOR_AGENT_ID
            for item in ResultsProjection.parse(run_id, payload, emitter=emitter):
                # RAW returned first (every parsed card the lane emitted).
                returned[credit_to] = returned.get(credit_to, 0) + 1
                if item.id in seen_ids:
                    continue
                keys = ResultsProjection.dedup_keys(item)
                if keys & seen_keys:
                    continue
                seen_ids.add(item.id)
                seen_keys |= keys
                counts[credit_to] = counts.get(credit_to, 0) + 1
                out.append(item)
        return out, counts, returned

    def _start_related_questions(
        self, run: RunRecord, answer_text: str, status: RunTerminalStatus
    ) -> _RelatedAsync | None:
        """Launch the parallel follow-up call on a daemon thread; ``None`` if off.

        Disabled (returns ``None``) when no :class:`RelatedQuestionsRunner` is
        armed (the legacy / test path — keeps settle LLM-free), when the run did
        not complete, or when there is no answer to base follow-ups on. The
        thread writes its result into a shared box the settle worker joins on.
        """
        if self._related_runner is None or status != "completed":
            return None
        if not answer_text.strip():
            return None
        runner = self._related_runner
        query = run.query
        box: dict[str, list[str]] = {}

        def _generate() -> None:
            box["q"] = runner.run(query, answer_text)

        thread = threading.Thread(
            target=_generate, name=f"related-q-{run.run_id}", daemon=True
        )
        thread.start()
        return _RelatedAsync(thread=thread, box=box)

    def _resolve_related_questions(
        self,
        related_async: _RelatedAsync | None,
        records: list[dict[str, Any]],
        streamer: RunEventStreamer | None,
    ) -> list[str]:
        """Join the parallel call (bounded), else fall back to the agent emit.

        The parallel structured call is the PRIMARY source; when it is off or
        yields nothing (LLM failure / timeout), fall back to the last
        ``scg_results`` emit's ``related_questions`` (the live capture last) so a
        run that the agent voluntarily annotated still surfaces follow-ups.
        """
        if related_async is not None:
            related_async.thread.join(timeout=_RELATED_Q_TIMEOUT_S)
            generated = related_async.box.get("q") or []
            if generated:
                return generated
        related = self._build_related_questions(records)
        if not related and streamer is not None:
            related = streamer.related_questions()
        return related

    @staticmethod
    def _build_related_questions(records: list[dict[str, Any]]) -> list[str]:
        """The last ``scg_results`` emit's ``related_questions`` (last write wins)."""
        related: list[str] = []
        for rec in records:
            if rec.get("type") != "tool_result":
                continue
            payload = rec.get("payload") or {}
            if not ResultsProjection.is_results_event(payload):
                continue
            questions = ResultsProjection.related_questions(payload)
            if questions:
                related = questions  # last write wins
        return related

    @staticmethod
    def _elapsed_ms(run: RunRecord) -> int:
        """Honest run duration: ``started_at``/``created_at`` ISO → now, in ms.

        Parses the run's start timestamp (``started_at`` preferred, ``created_at``
        fallback) and measures to settle time. ``0`` only when neither parses (a
        record with no start stamp) — never the old hardcoded ``0`` that read
        ``0ms`` beside a multi-minute run.
        """
        start_iso = run.started_at or run.created_at
        if not start_iso:
            return 0
        try:
            started = datetime.fromisoformat(start_iso)
            now = datetime.fromisoformat(utc_now_iso())
        except ValueError:
            return 0
        delta_ms = int((now - started).total_seconds() * 1000)
        return max(delta_ms, 0)

    @staticmethod
    def _synthesis_metrics(
        trace: list[TraceAgent], results: list[SearchResult]
    ) -> tuple[float, int]:
        """Derive ``(confidence, sources_count)`` from probes AND emitted results.

        Honest, defined provenance (see :class:`AnswerSynthesis`):

        * ``sources_count`` = the count of DISTINCT grounding sources = the union
          of (each data-bearing probe lane — keyed by its ``agent_id``, since one
          lane is one qualified-pathway walk and its wire ``source_id`` is the
          shared parent grouping key, not the connector) ∪ the emitted results'
          ``source`` fields. So a probe-only run reports its breadth, a
          root-inline run reports its distinct connectors, and a mixed run reports
          the union.
        * ``confidence`` — when probes ran: ``data-bearing probes / probes run``.
          When NO probe ran but results were emitted (the root-inline path): the
          mean of the emitted entries' folded score (``relevance``, carrying
          ``confidence`` when no explicit rank), rounded 2dp. Nothing ran AND
          nothing emitted → ``(0.0, 0)`` so the console keeps suppressing the
          chip. The coordinator lane is NOT a probe — never in the probe ratio.
        """
        with_data_probes = [a for a in trace if not ProbeTrace.is_dead_end(a.result)]
        sources = {f"probe:{a.agent_id}" for a in with_data_probes}
        sources |= {r.source for r in results if r.source}
        sources_count = len(sources)

        if trace:
            confidence = round(len(with_data_probes) / len(trace), 2)
        elif results:
            # No probes ran (root-inline): mean of the entries' folded score
            # (``relevance`` carries ``confidence`` when no explicit rank — see
            # ``ResultsProjection._to_result``).
            confidence = round(sum(r.relevance for r in results) / len(results), 2)
        else:
            return 0.0, 0
        return confidence, sources_count

    @staticmethod
    def _task_result(
        records: list[dict[str, Any]],
    ) -> tuple[str, str | None]:
        """Extract ``(answer_text, error)`` from the last ``completion`` event.

        Status is NOT derived here — that comes from ``summarize_session``
        (see :meth:`_run_status`). The summary doesn't carry ``task_result``,
        so the raw payload (``{done, done_reason, task_result, error?,
        last_error?}`` — orchestrator.py; there is no ``text`` key) is read for
        the synthesized answer + error detail only.
        """
        completion: dict[str, Any] | None = None
        for rec in records:
            if rec.get("type") == "completion":
                completion = rec.get("payload") or {}
        if completion is None:
            return "", "no completion event in transcript"
        text = str(completion.get("task_result") or "")
        err = completion.get("error") or completion.get("last_error")
        return text, str(err) if err else None

    # -- Failure terminal ---------------------------------------------------

    def _fail(
        self, run: RunRecord, *, store: Any, code: str, message: str
    ) -> RunPayload:
        """Append an ``error`` terminal + persist a failed snapshot; return it.

        The single failure path for every early-out, caught exception, and
        non-success terminal, so the event log always closes with exactly one
        terminal event (no second status channel). A no-op (beyond returning
        the failed payload) when the record is already terminal — the cancel
        route settles first and must never be followed by a second terminal.
        """
        payload = RunPayload(
            run_id=run.run_id,
            session_id=run.session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status="failed",
            tier=run.tier,
            model=run.model,
            total_ms=self._elapsed_ms(run),
            error=message,
        )
        if self._already_settled(store, run.run_id):
            return payload
        store.append_run_event(run.run_id, events.error(code=code, message=message))
        store.update_run(
            run.run_id,
            status="failed",
            completed_at=utc_now_iso(),
            # Persist the elapsed on the record too — the EVIDENCE: the settle +
            # fail ``update_run`` calls OMITTED ``total_ms`` so ``RunRecord``
            # kept its default 0 (the run_done event carried it, the record
            # lied). Both paths now record it.
            total_ms=payload.total_ms,
            error=message,
            payload=payload,
        )
        store.update_past_query(
            run.workspace_id, run.run_id, status="failed", results=0
        )
        return payload


__all__ = ["OrchestratedSearchRunner"]
