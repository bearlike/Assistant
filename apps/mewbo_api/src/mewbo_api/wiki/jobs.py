"""WikiIndexingJob + WikiQaSession — atomic orchestrator façades.

Atomic state lives in the WikiStore (jobs/qa collections + event logs).
These classes are thin Python facades for create/start/cancel/events.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from mewbo_core.agent_registry import parse_agent_file
from mewbo_core.common import get_logger
from mewbo_core.hooks import OutcomeAssertion
from mewbo_core.permissions import auto_approve
from mewbo_graph import plugins_root
from mewbo_graph.wiki.credentials import CredentialScope, CredentialStore
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.types import (
    QA_TERMINAL_STATUSES,
    IndexingJob,
    Project,
    ProjectSettings,
    QaAnswer,
    QaTurn,
    RepoCredential,
    WizardSubmission,
)

logging = get_logger(name="api.wiki.jobs")

# The ephemeral CloneTokenCache is gone (vestigial once every token was durably
# saved at submission via CredentialStore). The clone tool now resolves auth
# itself at clone time via the canonical chain
# (``mewbo_graph.wiki.credentials.resolve_chain`` — arg → repo-scoped store →
# host-scoped store → ambient git credential → anonymous), so this module only
# ever needs to WRITE the durable credential, never stash/restore it.

# Tools the wiki-indexer agent is allowed to call. Mirrors the AgentDef's
# frontmatter `tools:` list (wiki-indexer.md); these MUST stay in sync.
# NOTE: wiki_build_graph / wiki_query_graph are Phase-3 tools — tolerated
# when absent at runtime (the tool registry silently skips unknown names).
#
# `resolve_entity` (product decision): the playbook body (Step 5, "Construct
# PagePlan") instructs the root to make the plan entity-aware by consulting
# the entity graph via `resolve_entity` — documented, intended design (see
# packages/mewbo_graph/CLAUDE.md's GraphRAG ordering law), not incidental
# prose, and `tests/wiki/test_agent_defs.py::
# test_indexer_carries_entity_tools_and_enrich_step` already asserts it. Under
# `strict_tool_scope=True` (see `_start_indexer_session`) the allowlist is
# what decides what the root can call, so leaving this out would make the
# playbook instruct a call the ceiling forbids. It is a read-only lookup, so
# granting it widens nothing meaningfully. `mint_entity` / `relate_entities`
# stay OUT — those are WRITES the `wiki-enricher` child performs; a parent
# does not need a tool itself in order to grant it to a child (a child's
# specs are filtered against its OWN allowlist, never the parent's).
INDEXER_TOOLS: list[str] = [
    "wiki_clone_repo",
    "wiki_scan_tree",
    "wiki_load_grounder",
    "wiki_build_graph",      # Phase 3 — tolerated when absent
    "wiki_query_graph",      # Phase 3
    "wiki_graph_neighbors",  # Phase 3 — directed multi-hop traversal
    "resolve_entity",        # read-only entity lookup for entity-aware planning
    "wiki_commit_plan",
    "wiki_submit_page",      # also bound for sub-agents, but indexer can fall back
    "wiki_submit_insight",   # bootstrap the memory layer during indexing (flywheel)
    "wiki_finalize",
    "spawn_agent",
    "check_agents",
    "read_file",
    "glob",
    "grep",
    "ls",
]

# Directory of the bundled wiki AgentDef markdown, resolved from the graph
# package's own plugin root (works across wheels, editable installs, and
# source trees alike — no fragile parents[N] walk).
_WIKI_AGENTS_DIR = plugins_root() / "wiki" / "agents"


class WikiIndexingJob:
    """Static façade — all state lives in the WikiStore."""

    @staticmethod
    def start(
        submission: WizardSubmission,
        *,
        runtime: Any,
        hook_manager: Any = None,
    ) -> IndexingJob:
        """Create a job record + start the underlying Mewbo session.

        Returns the freshly-created ``IndexingJob`` (status ``queued``).
        The actual work happens asynchronously in the started session.
        """
        store: WikiStoreBase = runtime.wiki_store
        job_id = uuid.uuid4().hex
        try:
            host = urlparse(submission.repo_url).hostname or None
        except Exception:
            host = None

        # Persist the credential durably (keyed by slug) FIRST — before the job
        # record even exists. The credential is slug-keyed, independent of the
        # job, so saving it first means a crash between the two writes can never
        # leave a job that recovery can't authenticate (a job with no credential
        # is unrecoverable; a credential with no job is harmless). Strip the
        # token from the persisted submission below — the clone tool's
        # ``resolve_chain`` reads the credential back from HERE at clone time,
        # regardless of which process (or a later restart) drives the clone.
        scope = CredentialScope.coerce(submission.slug)
        if submission.token and scope is not None:
            CredentialStore.save(
                store,
                scope,
                RepoCredential(kind="token", value=submission.token, username=None),
            )
        elif submission.token:
            # An unparseable slug has no scope to key the credential by, and the
            # chain would never find it — say so instead of writing a token under
            # a key nothing can ever resolve (the clone then fails honestly).
            logging.warning("skipping durable credential: submission slug is not a valid scope")

        job = IndexingJob(
            jobId=job_id,
            slug=submission.slug,
            status="queued",
            scannedCount=0,
            totalCount=0,
            currentFile=None,
            platform=submission.platform,
            host=host,
            model=submission.model,
        )
        store.create_job(job)

        # Persist the submission MINUS the token (token only crosses the
        # wire to the clone tool, never the store).
        sub_dict = submission.model_dump(mode="json", by_alias=True, exclude_none=True)
        sub_dict.pop("token", None)
        store.save_job_submission(job_id, sub_dict)

        # Seed/refresh the SLUG-keyed settings record — the durable edit target.
        # The job-keyed sidecar above stays what it always was:
        # immutable history of what THIS job ran with. This one is what the
        # project is CONFIGURED with, so it is what ``refresh`` replays and what
        # ``PATCH /v1/wiki/projects/<slug>`` writes. An existing ``desc`` override
        # is carried forward — a refresh re-enters ``start`` with a reconstructed
        # submission (which has no desc), so rebuilding the record from the
        # submission alone would silently drop the user's edited description.
        existing_settings = store.get_project_settings(submission.slug)
        store.save_project_settings(
            submission.slug,
            ProjectSettings.from_submission(
                submission,
                desc=existing_settings.desc if existing_settings else None,
            ),
        )

        # Developer mode: a DETERMINISTIC, zero-LLM index (clone→scan→graph→
        # finalize, no enrich/plan/pages). No Mewbo session, no agent — drive
        # GraphOnlyIndexer on a daemon thread (mirrors start_async's async-by-
        # handle shape; the SSE/snapshot surfaces carry progress). ``graph_only``
        # is already forced False by the route when developer_mode is off, so
        # honouring it here is safe.
        if submission.graph_only:
            _start_graph_only_index(store=store, job_id=job_id, submission=submission)
            return job

        # Build user query that carries the submission contract. Pass the store
        # so the auth note reflects what is DURABLY on file (the credential saved
        # just above), not the — now stripped — submission.token.
        user_query = _render_user_query(submission, store=store)
        _start_indexer_session(
            store=store,
            runtime=runtime,
            job_id=job_id,
            model=submission.model,
            user_query=user_query,
            hook_manager=hook_manager,
            fallback_models=_fallback_ladder(submission),
        )
        return job

    @staticmethod
    def cancel(job_id: str, *, runtime: Any) -> bool:
        """Cancel a running indexing job.

        Returns ``True`` if a cancel event was appended; ``False`` if the
        job is unknown or was already cancelled.
        """
        store: WikiStoreBase = runtime.wiki_store
        session_id = store.get_job_session(job_id)
        appended = store.cancel_job(job_id)
        if appended and session_id:
            try:
                runtime.cancel(session_id)
            except Exception as exc:  # pragma: no cover — runtime cancel is best-effort
                logging.warning("runtime.cancel({}) failed: {}", session_id, exc)
        return appended

    @staticmethod
    def refresh(
        slug: str,
        *,
        runtime: Any,
        hook_manager: Any = None,
    ) -> IndexingJob:
        """Re-index an existing project (on-demand only) with a full rebuild.

        Rebuilds the whole wiki from a reconstructed ``WizardSubmission`` (the
        proven path; also re-bootstraps the memory layer as the indexer
        deposits insights while indexing). The on-demand incremental engine
        (``mewbo_graph.wiki.refresh.RefreshOrchestrator``) is the tested
        substrate for a future scoped-refresh ACT path; until it is wired in,
        every refresh does a full re-index.

        The submission is reconstructed from the slug-keyed ``ProjectSettings``
        record when one exists — that is what makes an edited model/ref/scope
        actually take effect — falling back to the legacy per-job
        sidecar scan, and finally to the ``Project`` record's own fields.

        Returns the freshly-created :class:`IndexingJob`.
        """
        store: WikiStoreBase = runtime.wiki_store

        # 1. Verify the project exists.
        project = store.get_project(slug)
        if project is None:
            raise KeyError(f"Project not found: {slug}")
        logging.info("wiki refresh slug={}", slug)

        # 2. Reconstruct the submission this re-index replays.
        #
        # The SLUG-keyed settings record is the authority: it is what
        # the project is configured with, and the ONE thing a settings PATCH
        # writes. Consulting it first is what makes an edit take effect here.
        #
        # A project onboarded before that record existed has none, so fall back to
        # the legacy scan of per-job submission sidecars. A sidecar that doesn't
        # validate as a WizardSubmission is skipped as belt-and-suspenders —
        # restart recovery keeps its retry counter on its OWN slug-keyed surface
        # (store.{get,bump}_recovery_attempts), so our own writes never pollute the
        # sidecar; this guard only protects a hand-edited / legacy one.
        settings = store.get_project_settings(slug)
        submission: WizardSubmission | None = (
            settings.to_submission()
            if settings is not None
            else _latest_job_submission(store, slug)
        )

        # 3. Build a WizardSubmission — prefer stored; fall back to project fields.
        from mewbo_core.config import get_config  # noqa: PLC0415

        default_model = default_index_model()
        if submission is None:
            submission = submission_from_project(project)

        # 3b. Guard against a stored model the proxy has since retired. A reindex
        # replays the stored submission's model verbatim, so a sticky model that
        # was valid when first chosen but later dropped from the proxy would
        # fast-fail the whole run on an invalid-model 400 (a regression seen
        # before). Re-resolve to the wiki/llm default when it's no longer
        # offered; RetryStrategy's switch-on-invalid-model is the in-loop backstop
        # if the proxy can't be reached here.
        resolved_model = get_config().llm.resolve_available_model(
            submission.model, fallback=default_model
        )
        if resolved_model and resolved_model != submission.model:
            logging.warning(
                "wiki refresh slug={}: stored model {!r} not offered by proxy; using {!r}",
                slug,
                submission.model,
                resolved_model,
            )
            submission = submission.model_copy(update={"model": resolved_model})

        # 4. Start the new indexing job.
        return WikiIndexingJob.start(submission, runtime=runtime, hook_manager=hook_manager)

    @staticmethod
    def events_since(
        job_id: str,
        after_idx: int,
        *,
        store: WikiStoreBase,
    ) -> list[dict[str, Any]]:
        """Return events with idx > ``after_idx`` for the given job."""
        return store.load_job_events(job_id, after_idx=after_idx)


# ---------------------------------------------------------------------------
# WikiQaSession
# ---------------------------------------------------------------------------

# Tools the wiki-qa HYPERVISOR (root) is allowed to call. Mirrors the
# wiki-qa.md frontmatter. The root does NOT retrieve directly — graph
# traversal, search, and file reads happen in the ``wiki-qa-probe`` sub-agents
# it fans out. (The old flat tool set let the root read one page and stop,
# never touching the graph or embeddings the wiki built; the probe fan-out is
# the fix.) ``spawn_agent``/``check_agents``/``steer_agent`` are injected for
# any depth-0 root regardless of strict scope.
#
# This list is a STRUCTURAL ceiling, not just the root's *visible* stateless
# surface: ``SessionToolRegistry.build_for`` treats a non-empty ``allowed_tools``
# as authoritative over the capability gate too (see its
# docstring), so the root genuinely cannot bind ``wiki_read_page``/
# ``wiki_query_graph``/etc. even though the session advertises the ``wiki``
# capability. Previously this "root never retrieves" invariant held only by prompt
# convention. The run's full scope (incl. why it self-approves) is applied in
# one place — ``WikiQaSession._dispatch_qa_run``.
QA_TOOLS: list[str] = [
    "wiki_list_pages",       # cheap orientation only — titles, not content
    "wiki_emit_answer",      # the ONE atomic call that delivers the whole answer
    "wiki_submit_insight",   # QA→memory flywheel (deposit a durable fact)
    "spawn_agent",           # fan out wiki-qa-probe retrieval probes
    "check_agents",          # collect probe findings
]

# Hard cost backstop on the QA fan-out. Probe count is prompt-guided
# (wiki-qa.md: "deploy as many as the question needs"), but an unbounded root
# spent ~1.1M tokens / 110 steps / 8+ probes on a 6-item question. This caps
# TOTAL tool steps across the root AND every wiki-qa-probe child (the hypervisor
# counts session-wide). Sized GENEROUS — a cost ceiling, not a tight cap: the
# happy path is ~13 steps, so 50 leaves wide headroom for a legitimately broad
# question while killing the 100+-step runaway.
QA_SESSION_STEP_BUDGET: int = 50

# Step headroom for the one-shot no-emit nudge re-drive (QaSessionEndHook):
# delivering an already-composed answer is ONE wiki_emit_answer call, so the
# nudge run needs only a few steps. Added ON TOP of the session budget so the
# re-drive isn't stillborn when the first run already spent its allowance.
QA_NUDGE_STEP_BUDGET: int = 8

# The corrective follow-up for a run that ended without emitting (the model
# narrated its emit call as text, or answered only in its reply). Domain-state
# triggered — see QaSessionEndHook._nudge_if_silent.
_QA_NUDGE: str = (
    "Your answer was NOT delivered: no wiki_emit_answer tool call was made, and "
    "reply text is discarded — the user has seen nothing. Deliver the complete "
    "answer NOW by CALLING the wiki_emit_answer tool once, with the full blocks "
    "array ending in the sources block. Never write the call out as text."
)


class WikiQaSession:
    """Static façade — all QA session state lives in the WikiStore."""

    @staticmethod
    def _dispatch_qa_run(
        *,
        runtime: Any,
        session_id: str,
        question: str,
        model: str,
        playbook: str,
        hook_manager: Any,
    ) -> str:
        """Start the read-only, QA-scoped agent run — ONE source of truth for scope.

        Shared by the cold :meth:`start` and the :meth:`follow_up` re-engage so the
        QA run's scope (``QA_TOOLS`` + ``strict_tool_scope`` ceiling + step budget)
        can't drift between the two entry points. The run is entirely read-only, so
        it self-approves (``auto_approve``, the posture every headless drive uses):
        the ``QA_TOOLS`` ceiling does the narrowing, so a restrictive callback would
        only park capability-surfaced read-only calls, never scope them. See
        the ``QA_TOOLS`` comment / ``build_for``. Returns ``start_async``'s run id
        (``""`` when a run is already in flight — the caller decides if that's fatal).

        ``enable_skills=False`` — this headless QA drive has no business
        inheriting the host's ``~/.claude`` skill directories; a bound skill
        advertised for a human's interactive session is pure distraction surface
        here (E-V).
        """
        return runtime.start_async(
            session_id=session_id,
            user_query=question,
            model_name=model,
            allowed_tools=QA_TOOLS,
            strict_tool_scope=True,
            approval_callback=auto_approve,
            skill_instructions=playbook,
            hook_manager=hook_manager,
            session_step_budget=QA_SESSION_STEP_BUDGET,
            enable_skills=False,
        )

    @staticmethod
    def start(
        *,
        slug: str,
        question: str,
        from_page_id: str,
        model: str,
        runtime: Any,
        hook_manager: Any = None,
    ) -> QaAnswer:
        """Create a QaAnswer record + start the underlying Mewbo session.

        Emits the ``meta`` event **synchronously** before calling
        ``start_async`` so the SSE consumer sees it as the very first event.
        """
        store: WikiStoreBase = runtime.wiki_store
        answer_id = uuid.uuid4().hex
        answer = QaAnswer(
            answerId=answer_id,
            fromPageId=from_page_id,
            question=question,
            summarySources=[],
            model=model,
            blocks=[],
            slug=slug,
        )
        store.save_qa(answer)

        session_tag = f"wiki:qa:{answer_id}"
        session_id = runtime.resolve_session(session_tag=session_tag)
        store.attach_qa_session(answer_id, session_id)

        playbook = _load_qa_playbook()
        # Advertise the ``wiki`` capability so the wiki-qa AgentDef stays
        # spawnable from inside this session (same gating as indexing), AND
        # persist the QA tool-scope/playbook/strict-scope as first-class
        # session state (Phase 1) rather than bare ``start_async``
        # kwargs — durable + inspectable (Mongo/Langfuse), instead of the prior
        # "not bare kwargs" gap where nothing on the session recorded what a
        # QA run was actually scoped to.
        runtime.append_context_event(session_id, {
            "client_capabilities": ["wiki"],
            "mcp_tools": QA_TOOLS,
            "strict_tool_scope": True,
            "session_step_budget": QA_SESSION_STEP_BUDGET,
            "skill_instructions": playbook,
        })

        # Emit meta immediately — before start_async — so the SSE generator
        # sees it as the first event regardless of how fast the agent runs.
        # Also marks the start of this turn's slice of the event log (see
        # ``QaFinalizer.current_turn_events``).
        store.append_qa_event(answer_id, {
            "type": "meta",
            "answerId": answer_id,
            "model": model,
            "fromPageId": from_page_id,
            "sessionId": session_id,
        })

        WikiQaSession._dispatch_qa_run(
            runtime=runtime,
            session_id=session_id,
            question=question,
            model=model,
            playbook=playbook,
            hook_manager=hook_manager,
        )
        return answer

    @staticmethod
    def follow_up(
        answer_id: str,
        question: str,
        *,
        runtime: Any,
        hook_manager: Any = None,
    ) -> QaAnswer:
        """Continue an existing QA answer's session with a new question.

        Reuses the SAME ``session_id``/session_tag :meth:`start` created — the
        underlying engine call (``runtime.start_async`` re-engaging an existing
        ``session_id``) is the exact primitive the generic session-continuation
        path (``send_followup`` / ``POST /api/sessions/<id>/message``) uses to
        resume an idle session, and the one :meth:`QaSessionEndHook._nudge_if_silent`
        already uses to re-drive this same session for its corrective nudge —
        this is the QA-domain entry point onto that engine, not a second,
        parallel one (Phase 1).

        The PRIOR turn — already reconciled onto the snapshot by
        ``QaFinalizer.close`` when its run ended — is snapshotted into
        ``turns`` and the top-level fields are reset for the new turn. No new
        ``answer_id`` is minted; the SSE stream for this turn is the same
        ``WikiQaSseGenerator(answer_id=answer_id)`` a caller already uses.

        Raises ``LookupError`` if *answer_id* has no backing session, and
        ``RuntimeError`` if that session already has a run in flight.
        """
        store: WikiStoreBase = runtime.wiki_store
        prior = store.get_qa(answer_id)
        session_id = store.get_qa_session(answer_id)
        if prior is None or not session_id:
            raise LookupError(f"no QA session for answer {answer_id}")
        # Guard BEFORE any mutation (mirrors SessionRecovery.post, backend.py):
        # a double-submit/retry racing the still-streaming prior turn must
        # bail out before touching the store — appending a new ``meta``/
        # resetting ``turns`` while that turn is still live would shift
        # QaFinalizer.current_turn_events' boundary mid-stream and corrupt it.
        # The late ``run_id`` check below stays too, as TOCTOU defense-in-depth
        # for the gap between this check and the actual start_async attempt.
        if runtime.is_running(session_id):
            raise RuntimeError(f"QA session {session_id} is already running")

        prior_turn = QaTurn(
            question=prior.question,
            blocks=prior.blocks,
            summarySources=prior.summary_sources,
            accessedSources=prior.accessed_sources,
            modelsUsed=prior.models_used,
            status=prior.status,
        )
        updated = prior.model_copy(update={
            "question": question,
            "blocks": [],
            "summary_sources": [],
            "accessed_sources": [],
            "status": "running",
            "turns": [*prior.turns, prior_turn],
        })
        # NON-destructive — same rule as WikiQaSession.cancel/QaFinalizer.close:
        # save_qa would reset the Mongo event_count + drop session_id.
        store.update_qa_fields(updated)

        # New turn boundary on the event log — see QaFinalizer.current_turn_events.
        store.append_qa_event(answer_id, {
            "type": "meta",
            "answerId": answer_id,
            "model": updated.model,
            "fromPageId": updated.from_page_id,
            "sessionId": session_id,
        })

        run_id = WikiQaSession._dispatch_qa_run(
            runtime=runtime,
            session_id=session_id,
            question=question,
            model=updated.model,
            playbook=_load_qa_playbook(),
            hook_manager=hook_manager,
        )
        if not run_id:
            raise RuntimeError(f"QA session {session_id} is already running")
        return updated

    @staticmethod
    def cancel(answer_id: str, *, runtime: Any) -> bool:
        """Cancel a running QA session.

        Returns ``True`` if a ``cancelled`` event was appended;
        ``False`` if already cancelled (idempotent).
        """
        store: WikiStoreBase = runtime.wiki_store
        session_id = store.get_qa_session(answer_id)

        # Idempotency check — don't append a second cancelled event.
        existing = store.load_qa_events(answer_id)
        if any(e.get("type") == "cancelled" for e in existing):
            return False

        store.append_qa_event(answer_id, {"type": "cancelled"})
        # Mark the answer snapshot terminal too, so a non-streaming consumer
        # (the MCP ask_wiki poll) stops instead of waiting out its timeout.
        # NON-destructive update (never save_qa — that resets the Mongo
        # event_count + drops session_id; same rule as QaFinalizer.close).
        try:
            snap = store.get_qa(answer_id)
            if snap is not None and snap.status not in QA_TERMINAL_STATUSES:
                data = snap.model_dump(by_alias=True)
                data["status"] = "cancelled"
                store.update_qa_fields(QaAnswer.model_validate(data))
        except Exception as exc:  # pragma: no cover — best-effort snapshot honesty
            logging.warning("marking {} cancelled on snapshot failed: {}", answer_id, exc)
        if session_id:
            try:
                runtime.cancel(session_id)
            except Exception as exc:  # pragma: no cover — best-effort
                logging.warning("runtime.cancel({}) failed: {}", session_id, exc)
        return True


class WikiIndexingSessionEndHook:
    """Mark a non-terminal indexing job ``interrupted`` when its session ends.

    Defense-in-depth net for tool-internal infra failures and the
    general case where a session ends without reaching ``wiki_finalize``:

    - Happy path (``wiki_finalize`` succeeded): the job is already ``complete``
      → this hook no-ops.
    - Infra failure (e.g. ``wiki_build_graph`` network error): the LLM catches
      the tool error and exits cleanly (``done_reason="completed"`` with an
      error field), but the wiki job is still ``scanning``/``queued``/etc.
      → this hook marks it ``interrupted`` so ``JobRecovery`` picks it up on
      the next restart via the existing checkpoint-aware ``WikiResume`` path.

    Fires for EVERY session end; a non-indexing session (``find_job_by_session``
    → ``None``) is a cheap no-op. A failing hook must NEVER block.

    **Reconciliation.** ``complete`` and ``cancelled`` are the statuses this
    hook treats as genuinely settled: a cancel is a deliberate user stop, so the
    session wrapping up cleanly right after is the CORRECT outcome, not a
    mismatch. For any OTHER status (``failed``, or still non-terminal —
    ``queued``/``scanning``/``finalizing``/``interrupted``) the job never
    reached ``wiki_finalize``, so a session that nonetheless ends with
    ``error=None`` (a clean ``done_reason``, per ``summarize_session``)
    disagrees with the job it belongs to: the model exited believing it
    finished, while the artifact it was building never reached its terminal
    state. This is the majority laundering case measured live — 17 of 33
    wiki-index sessions marked ``completed`` whose job was NOT ``complete``,
    most having died mid-``clone``.

    Returned as an :class:`~mewbo_core.hooks.OutcomeAssertion` so
    ``summarize_session`` can promote the session's own derived status from
    ``completed`` to ``unmet_goal`` — the return channel
    ``HookManager.run_on_session_end`` now collects. This hook is scoped to
    "no exception, but the goal wasn't met"; a true repo/network/quota block
    is a DIFFERENT case already covered by the loop's own ``blocked_code``
    path and must not be duplicated here. The job's own event log (this
    package's surface) still gets a matching entry — the SESSION-level
    assertion and the JOB-level record serve different readers (a session
    status badge vs. job forensics) and neither replaces the other.
    """

    # The only statuses this hook treats as genuinely settled — no assertion,
    # no interrupted handoff.
    _TERMINAL: frozenset[str] = frozenset({"complete", "cancelled"})
    # Terminal for the JOB on its own (never re-driven by recovery), but still
    # eligible for the outcome assertion below when the session ended clean.
    _FAILED: str = "failed"

    def __init__(self, runtime: Any) -> None:
        """Store a reference to the runtime for store access."""
        self._runtime = runtime

    def __call__(self, session_id: str, error: str | None) -> OutcomeAssertion | None:
        """The ``on_session_end`` callback."""
        try:
            store = self._runtime.wiki_store
            job_id = store.find_job_by_session(session_id)
            if not job_id:
                return None
            job = store.get_job(job_id)
            if job is None or job.status in self._TERMINAL:
                return None

            # The job never reached terminal complete. If the session itself
            # ended clean (no exception the loop's own signals caught), its
            # derived status would otherwise read "completed" while the job
            # disagrees — report the mismatch so it can be promoted instead.
            assertion: OutcomeAssertion | None = None
            if error is None:
                assertion = OutcomeAssertion(
                    reason="job_not_complete",
                    detail=f"wiki indexing job status={job.status!r} phase={job.phase!r}",
                )
                store.append_job_event(job_id, {
                    "type": "log",
                    "level": "warning",
                    "text": (
                        f"session {session_id} ended cleanly, but job {job_id} "
                        f"never reached terminal complete (status={job.status}) "
                        "— outcome-assertion mismatch"
                    ),
                })
                logging.warning(
                    "wiki indexing session-end: job {} is {} but session {} "
                    "ended clean (outcome-assertion mismatch)",
                    job_id, job.status, session_id,
                )

            if job.status == self._FAILED:
                # Already terminal on its own — don't touch job.status.
                return assertion

            # Non-terminal job whose session ended — hand off to restart
            # recovery regardless of whether an assertion was raised above
            # (an errored session still needs this handoff).
            store.update_job(job_id, status="interrupted")
            logging.info(
                "wiki indexing session-end: marked job {} interrupted (was {})",
                job_id, job.status,
            )
            return assertion
        except Exception:  # pragma: no cover — a session-end hook never blocks
            logging.warning("wiki indexing session-end hook failed", exc_info=True)
            return None


def _load_qa_playbook() -> str:
    """Read the wiki-qa.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-qa.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-qa.md not found at {}", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


class QaSessionEndHook:
    """Finalize a wiki-QA answer when its backing session ends.

    The QA counterpart to indexing's ``wiki_finalize`` tool: the hypervisor's
    terminal ``wiki_emit_answer`` already closes the happy path (snapshot reconcile +
    ``complete``), but a run that *ends without* that call leaves the answer open.
    This atomic adapter (DI'd runtime + hook manager) is registered on
    ``HookManager.on_session_end`` and is the net for that: it also stamps the one
    piece of provenance that needs the transport layer — the distinct ``models_used``
    that ran across the hypervisor + its probes, read from the session transcript
    (the down-layer ``QaFinalizer`` owns everything derivable from the QA log).

    A no-error run that emitted NOTHING gets exactly ONE corrective re-drive first
    (see :meth:`_nudge_if_silent`) — the observed failure mode is a model composing
    the full answer but narrating the emit call as plain text, which the loop ends
    silently. The guard is domain state (zero ``block_open`` events), never text
    sniffing; a still-empty nudged run closes as an honest ``error``.

    Fires for EVERY session end; a non-QA session (``find_qa_by_session`` → ``None``)
    is a cheap no-op. ``error`` is non-None when the run halted → honest terminal state.
    """

    def __init__(self, runtime: Any, hook_manager: Any = None) -> None:
        """Inject the runtime (wiki store + transcript reader) + the hook manager.

        ``hook_manager`` is re-attached to the nudge re-drive so the nudged run's
        end re-enters this net (and the marker event bounds it to one pass).
        """
        self._runtime = runtime
        self._hook_manager = hook_manager

    def __call__(self, session_id: str, error: str | None) -> None:
        """The ``on_session_end`` callback — never raises (a failing hook must not block)."""
        from mewbo_graph.wiki.qa import QaFinalizer, QaMemoryDepositor  # noqa: PLC0415

        try:
            store = self._runtime.wiki_store
            answer_id = store.find_qa_by_session(session_id)
            if not answer_id:
                return
            if self._nudge_if_silent(store, answer_id, session_id, error):
                return  # one corrective re-drive in flight; its end re-enters here
            QaFinalizer.enrich(store, answer_id, models=self._models_used(session_id))
            QaFinalizer.close(store, answer_id, error)
            # Post-QA memory flywheel: distill the finalized answer into a refined
            # memory note grafted onto the multiplex graph (best-effort, off the
            # user's latency path — the answer is already delivered). Read the
            # snapshot AFTER close() so blocks/sources are reconciled. The
            # question text is not cheaply available in this hook (it lives in the
            # session transcript, not the QA snapshot) → the depositor works from
            # the answer alone.
            snap = store.get_qa(answer_id)
            if snap is not None:
                QaMemoryDepositor.deposit(store, snap, question=None)
        except Exception:  # pragma: no cover — a session-end hook never blocks
            logging.warning("wiki QA session-end finalize failed", exc_info=True)

    def _nudge_if_silent(
        self, store: Any, answer_id: str, session_id: str, error: str | None
    ) -> bool:
        """One bounded re-drive when a no-error run ended without emitting.

        Returns True when the nudge was dispatched (the caller must NOT close).
        The ``nudge`` marker event bounds this to exactly one retry; error,
        cancelled, already-emitted, and already-nudged runs all fall through.
        Scoped to the CURRENT TURN's slice of the log — else a
        continued session's silent-check would see a PRIOR turn's
        ``block_open``/``complete`` and wrongly skip the nudge (or vice versa).
        """
        from mewbo_graph.wiki.qa import QaFinalizer  # noqa: PLC0415

        if error is not None:
            return False
        events = QaFinalizer.current_turn_events(store.load_qa_events(answer_id))
        skip = ("block_open", "nudge", "complete", "cancelled", "error")
        if any(ev.get("type") in skip for ev in events):
            return False
        store.append_qa_event(answer_id, {"type": "nudge"})
        snap = store.get_qa(answer_id)
        logging.info("wiki QA {} ended silent — one corrective re-drive", answer_id)
        self._runtime.start_async(
            session_id=session_id,
            user_query=_QA_NUDGE,
            model_name=snap.model if snap else None,
            allowed_tools=QA_TOOLS,
            strict_tool_scope=True,
            approval_callback=auto_approve,
            skill_instructions=_load_qa_playbook(),
            hook_manager=self._hook_manager,
            session_step_budget=QA_SESSION_STEP_BUDGET + QA_NUDGE_STEP_BUDGET,
            enable_skills=False,
        )
        return True

    def _models_used(self, session_id: str) -> list[str]:
        """Distinct models from the session transcript (root + every probe), in first-seen order."""
        models: list[str] = []
        try:
            for ev in self._runtime.load_events(session_id):
                if ev.get("type") in ("llm_call_start", "llm_call_end"):
                    model = (ev.get("payload") or {}).get("model")
                    if model and model not in models:
                        models.append(model)
        except Exception:  # pragma: no cover — provenance is best-effort
            pass
        return models


# ---------------------------------------------------------------------------
# WikiIndexingJob helpers
# ---------------------------------------------------------------------------


def _start_indexer_session(
    *,
    store: WikiStoreBase,
    runtime: Any,
    job_id: str,
    model: str,
    user_query: str,
    hook_manager: Any = None,
    fallback_models: tuple[str, ...] | None = None,
) -> str:
    """Create + start the wiki-indexer session for *job_id*; return the session id.

    The single chokepoint for the "resolve a ``wiki:job:<id>`` session, advertise
    the ``wiki`` capability, prepare the clone dir, and ``start_async`` the indexer
    with INDEXER_TOOLS + the playbook" sequence — shared by :meth:`start` and the
    checkpoint-aware :class:`WikiResume.resume` so the capability advertisement,
    tool allowlist, approval callback, and model-routing options can never drift
    between the two paths (advertising the wrong capability is the classic
    "stuck after scan" bug).

    Resume guidance (``ResumePlan.summary()``) rides the *user_query* task
    description (see ``_render_resume_query``), not a separate system-prompt slot.

    ``strict_tool_scope=True`` makes ``INDEXER_TOOLS`` authoritative: previously
    the indexer ran on the PERMISSIVE branch, which unions every non-MCP builtin
    back in regardless of this allowlist — the AgentDef's own
    ``disallowedTools: [exit_plan_mode, activate_skill]`` frontmatter was already
    silently discarded by ``_load_indexer_playbook`` (body-only), so neither layer
    was actually enforcing a ceiling. Under strict scope INDEXER_TOOLS alone
    decides what the indexer can call, which also makes that stale frontmatter
    line moot rather than wrong (both tools it names are absent from
    INDEXER_TOOLS anyway). ``enable_skills=False`` keeps this headless product
    run from inheriting host ``~/.claude`` skill directories — a wiki indexer has
    no business advertising a machine-local skill set.
    """
    session_tag = f"wiki:job:{job_id}"
    session_id = runtime.resolve_session(session_tag=session_tag)
    store.attach_job_session(job_id, session_id)

    # Advertise the ``wiki`` capability so the agent_registry exposes wiki-*
    # AgentDefs (wiki-indexer, wiki-page-writer, wiki-enricher, wiki-qa) to
    # spawn_agent lookups. Without this, the indexer's per-page/enrich spawns
    # return "Unknown agent type 'wiki-page-writer'".
    runtime.append_context_event(session_id, {"client_capabilities": ["wiki"]})

    # Prepare clone dir (same root the wiki tools resolve from job_id).
    clone_root = os.environ.get("MEWBO_WIKI_CLONE_ROOT") or "/tmp/mewbo/wiki/clones"
    cwd = str(Path(clone_root) / job_id)
    Path(cwd).mkdir(parents=True, exist_ok=True)

    skill_instructions = _load_indexer_playbook()

    runtime.start_async(
        session_id=session_id,
        user_query=user_query,
        model_name=model,
        fallback_models=fallback_models,
        allowed_tools=INDEXER_TOOLS,
        strict_tool_scope=True,
        skill_instructions=skill_instructions,
        cwd=cwd,
        hook_manager=hook_manager,
        approval_callback=auto_approve,
        enable_skills=False,
    )
    return session_id


def _start_graph_only_index(
    *,
    store: WikiStoreBase,
    job_id: str,
    submission: WizardSubmission,
) -> None:
    """Drive a deterministic (zero-LLM) graph-only index on a daemon thread.

    The developer-mode counterpart to ``_start_indexer_session``: no Mewbo
    session, no ``wiki-indexer`` playbook. Builds the job ctx via the down-only
    seam and runs :class:`GraphOnlyIndexer` off the request path so the route
    returns immediately (async-by-handle, like ``start_async``). The indexer
    emits the same phase/log/complete events the agent path does, so the SSE
    stream + landing card render progress identically.
    """
    import threading  # noqa: PLC0415

    from mewbo_graph.plugins.wiki.graph_only import (  # noqa: PLC0415
        GraphOnlyIndexer,
        build_graph_only_ctx,
    )

    ctx = build_graph_only_ctx(job_id=job_id, slug=submission.slug, store=store)
    indexer = GraphOnlyIndexer(ctx, submission)
    threading.Thread(
        target=indexer.run, name=f"graph-only-index-{job_id}", daemon=True
    ).start()


def _load_indexer_playbook() -> str:
    """Read the wiki-indexer.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-indexer.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-indexer.md not found at {}", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


def _load_job_submission(store: WikiStoreBase, job_id: str) -> WizardSubmission | None:
    """Reconstruct the persisted ``WizardSubmission`` sidecar for *job_id*, if any.

    Shared by :func:`_render_resume_query` and :class:`WikiResume.resume` (via the
    ``fallback_models`` ladder read) so "what did this job actually submit" has
    ONE parse path — an invalid/missing sidecar yields ``None`` in both callers
    rather than each re-deriving its own tolerance for a bad record.
    """
    raw = store.get_job_submission(job_id)
    if not raw:
        return None
    try:
        return WizardSubmission.model_validate(raw)
    except Exception:
        return None


def _fallback_ladder(submission: WizardSubmission | None) -> tuple[str, ...] | None:
    """The cross-model fallback ladder a submission opts into, or ``None``.

    ``None`` means "defer to the configured fallback policy"
    (``Orchestrator.__init__`` — ``fallback_models=None`` inherits
    ``effective_fallback_models()``); an empty/absent ladder also means that, so
    both collapse to ``None`` here rather than threading an empty tuple through.
    """
    if submission is None or not submission.fallback_models:
        return None
    return tuple(m.strip() for m in submission.fallback_models if m.strip()) or None


def _render_resume_query(store: WikiStoreBase, job: IndexingJob, plan: Any) -> str:
    """Render the user-query string for a checkpoint-aware RESUME of *job*.

    Reconstructs the submission contract from the stored submission (token-less;
    the clone tool re-resolves the credential from the durable store). Falls back
    to the project/job fields when no submission sidecar validates.

    Carries NO ``ref:`` line — the commit pin is no longer a prompt-level
    instruction. ``wiki_clone_repo`` resolves ``job.commit_sha`` SERVER-SIDE and
    ignores any ``ref`` the model supplies once a job has recorded one (every
    resume), so a rendered ``ref:`` note here would tell the model to pass a
    value the clone tool silently overrides — actively misleading, not merely
    redundant.
    """
    submission = _load_job_submission(store, job.job_id)

    repo_url = submission.repo_url if submission else None
    platform = submission.platform if submission else job.platform
    depth = submission.depth if submission else "comprehensive"
    language = submission.language if submission else "en"
    filter_mode = submission.filter_mode if submission else "exclude"
    dirs = submission.dirs if submission else []
    files = submission.files if submission else []

    return (
        "RESUME an interrupted auto-generated index of this repository.\n\n"
        "SUBMISSION:\n"
        f"  repoUrl: {repo_url}\n"
        f"  slug: {job.slug}\n"
        f"  platform: {platform}\n"
        f"  depth: {depth}\n"
        f"  language: {language}\n"
        f"  model: {job.model}\n"
        f"  filterMode: {filter_mode}\n"
        f"  dirs: {dirs}\n"
        f"  files: {files}\n"
        "  auth: <token resolved server-side — call wiki_clone_repo with token=null>\n"
        "\n"
        + plan.summary()
        + "\n\nProceed per the wiki-indexer playbook, honouring the RESUME guidance above."
    )


def default_index_model() -> str:
    """The model an index runs on when none is stored: wiki default → llm default.

    Mirrors the wizard route's chain (the Q&A chain is the separate
    ``routes._resolve_qa_model``). Shared by ``WikiIndexingJob.refresh`` and the
    settings façade so a project with no stored model and its settings DTO can
    never disagree about what the next index would actually use.
    """
    from mewbo_core.config import get_config_value  # noqa: PLC0415

    return str(
        get_config_value("wiki", "default_model", default="")
        or get_config_value("llm", "default_model", default="anthropic/claude-sonnet-4-6")
    )


def submission_from_project(project: Project) -> WizardSubmission:
    """Derive a minimal ``WizardSubmission`` from a ``Project`` record.

    The LAST-RESORT tier (below the settings record and the legacy sidecar scan):
    a project so old — or so damaged — that nothing recorded how it was indexed.
    Shared by ``WikiIndexingJob.refresh`` and the settings façade, so the settings
    a user is SHOWN for such a project are the same ones a refresh would run with.
    """
    slug = project.slug
    repo_url = project.repo_url or (
        f"https://github.com/{slug}" if project.source == "github" else slug
    )
    return WizardSubmission.model_validate({
        "repoUrl": repo_url,
        "slug": slug,
        "platform": project.source,
        "depth": "comprehensive",
        "language": project.lang,
        "model": default_index_model(),
        "filterMode": "exclude",
        "dirs": [],
        "files": [],
        "graphOnly": project.graph_only,
    })


def _latest_job_submission(store: WikiStoreBase, slug: str) -> WizardSubmission | None:
    """The most recent valid ``WizardSubmission`` sidecar for *slug* (legacy path).

    The BACKWARD-COMPAT tier of ``WikiIndexingJob.refresh``: a project onboarded
    before the slug-keyed ``ProjectSettings`` record existed has its settings only
    in the per-job sidecars, so we scan them newest-first.

    "Newest" is ordered by ``phase_started_at`` (ISO-8601, so lexicographic ==
    chronological), NOT by ``job_id``. ``job_id`` is a ``uuid4`` hex — it sorts
    RANDOMLY, so the old ``key=lambda j: j.job_id`` reverse-sort did not pick the
    latest submission at all, it picked an arbitrary one. That is the same trap
    the freshness route already documents for its baseline-sha lookup; `IndexingJob`
    has no ``created_at``, and this is the one ordering signal it does carry. A job
    that never emitted a phase (no timestamp) sorts last rather than winning by
    accident.
    """
    from pydantic import ValidationError  # noqa: PLC0415

    jobs = sorted(
        store.list_jobs(slug=slug),
        key=lambda j: j.phase_started_at or "",
        reverse=True,
    )
    for job in jobs:
        candidate = store.get_job_submission(job.job_id)
        if not candidate:
            continue
        try:
            return WizardSubmission.model_validate(candidate)
        except ValidationError:
            continue
    return None


def _durable_credential_scope(
    store: WikiStoreBase | None, slug: str
) -> CredentialScope | None:
    """The scope a durable credential for *slug* is actually stored under, if any.

    Walks the non-anonymous durable tiers of ``resolve_chain`` (repo scope → host
    scope — the scope's own ``host_scope()`` sharing rule) WITHOUT the ambient
    ``git credential fill`` subprocess: cheap enough for a per-render check, and
    enough to decide the auth note honestly.

    Returns the WINNING scope (not the credential) so a caller can say *where* the
    credential is on file without ever touching its value. ``None`` = nothing
    durable on file (the clone would go ambient/anonymous).
    """
    scope = CredentialScope.coerce(slug)
    if store is None or scope is None:
        return None
    host_scope = scope.host_scope()
    # A slug that IS a bare host collapses the two tiers into one — don't pay for
    # the same store read twice.
    candidates = [scope] if host_scope == scope else [scope, host_scope]
    try:
        for candidate in candidates:
            if CredentialStore.load(store, candidate) is not None:
                return candidate
    except Exception:  # pragma: no cover — a store read hiccup shouldn't block a render
        return None
    return None


def _durable_credential_present(store: WikiStoreBase | None, slug: str) -> bool:
    """True iff a durable credential is on file for *slug*'s repo OR host scope.

    The boolean projection of :func:`_durable_credential_scope` — one tier walk
    backs both the indexer's auth note and the settings route's credential status.
    """
    return _durable_credential_scope(store, slug) is not None


def _render_user_query(
    submission: WizardSubmission, *, store: WikiStoreBase | None = None
) -> str:
    """Render the user-query string the indexer agent receives.

    The auth note reflects what is DURABLY on file (the credential the clone
    chain will resolve), NOT ``submission.token``: refresh reconstructs a
    token-less submission, so keying the note off the token wrongly rendered
    "public repo" for a private repo on every refresh. Either way
    the indexer calls ``wiki_clone_repo`` with ``token=null`` — the credential is
    resolved server-side and never passed by the LLM.
    """
    token_note = (
        "  auth: <credential on file server-side — call wiki_clone_repo with token=null>\n"
        if _durable_credential_present(store, submission.slug)
        else "  auth: <none on file — public repo assumed; call wiki_clone_repo with token=null>\n"
    )
    # Only render the ref line when a branch/tag/sha was chosen; an omitted ref
    # keeps the query byte-identical to the default-branch behaviour.
    ref_note = f"  ref: {submission.ref}\n" if submission.ref else ""
    return (
        "Index this repository as an auto-generated documentation site.\n\n"
        "SUBMISSION:\n"
        f"  repoUrl: {submission.repo_url}\n"
        f"  slug: {submission.slug}\n"
        + ref_note
        + f"  platform: {submission.platform}\n"
        f"  depth: {submission.depth}\n"
        f"  language: {submission.language}\n"
        f"  model: {submission.model}\n"
        f"  filterMode: {submission.filter_mode}\n"
        f"  dirs: {submission.dirs}\n"
        f"  files: {submission.files}\n"
        + token_note
        + "\nProceed per the wiki-indexer playbook."
    )
