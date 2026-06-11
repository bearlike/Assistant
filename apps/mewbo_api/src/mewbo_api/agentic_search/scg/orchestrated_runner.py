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
    SearchResult,
    TraceAgent,
    Workspace,
    utc_now_iso,
)
from .config import ScgConfig
from .playbooks import load_playbook
from .run_streamer import ProbeTrace, RunEventStreamer
from .workspace_binding import WorkspaceGraphBinding

logging = get_logger(name="api.agentic_search.scg.orchestrated_runner")

RunTerminalStatus = Literal["completed", "failed", "cancelled"]


class OrchestratedSearchRunner:
    """Async ``SearchRunner`` backed by a real ``scg-search`` session.

    Dependency-light by design: the only collaborator is the ``SessionRuntime``
    passed through ``start(..., runtime=...)`` (so tests inject a fake runtime
    feeding a canned transcript — no LLM, no real session). State per run lives
    on the store's event log + record, not on the instance — including the tier
    (the budget knob rides ``RunRecord.tier``, never the runner) — so one
    runner is reusable.
    """

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

        # The workspace binding seam (#77): the ONE place a workspace confers the
        # ``scg`` capability + graph traversal tools + the source scope. The same
        # seam the structured graph-first path reuses.
        binding = WorkspaceGraphBinding.for_workspace(workspace, run.allowed_tools)

        try:
            session_id = self._seed_session(
                run, binding, runtime=runtime, source_platform=source_platform
            )
        except Exception as exc:  # noqa: BLE001 — surface as a structured error
            logging.warning("scg-search run %s failed to seed: %s", run.run_id, exc)
            return self._fail(run, store=store, code="internal", message=str(exc))

        # Patch the REAL session id before returning so the cancel route can
        # reach the registry handle while the worker drives.
        run = run.model_copy(update={"session_id": session_id})
        store.update_run(run.run_id, session_id=session_id)

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
                        # The tier picks the brain (fast→nano / auto→sonnet /
                        # deep→frontier via scg.traversal.tier_models); None
                        # (blank/unknown) falls back to llm.default_model.
                        # Probes inherit the session model.
                        model_name=ScgConfig.model_for_tier(run.tier),
                        allowed_tools=binding.allowed_tools(),
                        skill_instructions=load_playbook("scg-search"),
                        approval_callback=auto_approve,
                        should_cancel=cancel_event.is_set,
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
        if streamer is not None:
            streamer.reconcile_missing(trace)
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
                        agent_id=agent.agent_id, results_count=0, empty=not agent.lines
                    ),
                )

        # Results: the SCG search synthesizes a cited answer rather than emitting
        # per-source result cards (the connector return is the verifier, not a
        # normalized hit list); ``results`` stays empty until a probe contract
        # carries them. The trace + answer are the live surfaces today.
        results: list[SearchResult] = []

        status = self._run_status(summary)
        answer_text, err = self._task_result(records)
        answer = AnswerSynthesis(tldr=answer_text, sources_count=len(results))

        if status == "failed":
            return self._fail(
                run,
                store=store,
                code="agent_error",
                message=err
                or f"run ended: {summary.get('done_reason') or 'unknown'}",
            )

        if status == "completed":
            for chunk in _typewriter_chunks(answer_text):
                if chunk:
                    store.append_run_event(run.run_id, events.answer_delta(text=chunk))
            store.append_run_event(run.run_id, events.answer_ready(answer=answer))

        store.append_run_event(
            run.run_id, events.run_done(status=status, total_ms=0)
        )
        payload = RunPayload(
            run_id=run.run_id,
            session_id=session_id,
            query=run.query,
            workspace_id=run.workspace_id,
            status=status,
            tier=run.tier,
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
            if agent_id not in agents:
                order.append(agent_id)
                agents[agent_id] = TraceAgent(
                    id=agent_id,
                    agent_id=agent_id,
                    name=ProbeTrace.lane_name(payload),
                    source_id=ProbeTrace.source_id(payload),
                    slot=len(order) - 1,
                )
            # ONE projection shared with the live streamer (DRY) so a settle-time
            # reconciled lane is byte-identical to one that streamed live.
            agents[agent_id].lines.append(ProbeTrace.line(payload))
        return [agents[a] for a in order]

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
            error=message,
        )
        if self._already_settled(store, run.run_id):
            return payload
        store.append_run_event(run.run_id, events.error(code=code, message=message))
        store.update_run(
            run.run_id,
            status="failed",
            completed_at=utc_now_iso(),
            error=message,
            payload=payload,
        )
        store.update_past_query(
            run.workspace_id, run.run_id, status="failed", results=0
        )
        return payload


__all__ = ["OrchestratedSearchRunner"]
