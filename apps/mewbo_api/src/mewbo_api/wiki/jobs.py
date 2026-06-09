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
from mewbo_core.permissions import auto_approve
from mewbo_graph import plugins_root
from mewbo_graph.wiki.credentials import CredentialStore
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.tokens import CloneTokenCache
from mewbo_graph.wiki.types import (
    QA_TERMINAL_STATUSES,
    IndexingJob,
    QaAnswer,
    RepoCredential,
    WizardSubmission,
)

logging = get_logger(name="api.wiki.jobs")

# The ephemeral clone-token cache moved to ``mewbo_graph.wiki.tokens`` (Gitea
# #25) so the relocated clone/finalize tools share it via a down-only import
# instead of reaching up into this module.

# Tools the wiki-indexer agent is allowed to call. Mirrors the AgentDef's
# frontmatter `tools:` list (wiki-indexer.md); these MUST stay in sync.
# NOTE: wiki_build_graph / wiki_query_graph are Phase-3 tools — tolerated
# when absent at runtime (the tool registry silently skips unknown names).
INDEXER_TOOLS: list[str] = [
    "wiki_clone_repo",
    "wiki_scan_tree",
    "wiki_load_grounder",
    "wiki_build_graph",      # Phase 3 — tolerated when absent
    "wiki_query_graph",      # Phase 3
    "wiki_graph_neighbors",  # Phase 3 — directed multi-hop traversal
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
        # token from the persisted submission below — re-index reads it back
        # from here when the process that warmed CloneTokenCache is long gone.
        if submission.token:
            CredentialStore.save(
                store,
                submission.slug,
                RepoCredential(kind="token", value=submission.token, username=None),
            )

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

        # Build user query that carries the submission contract.
        user_query = _render_user_query(submission)

        # Stash the token for wiki_clone_repo to read — keeps it out of the
        # LLM transcript while still letting the tool authenticate the clone.
        if submission.token:
            CloneTokenCache.store(job_id, submission.token)

        _start_indexer_session(
            store=store,
            runtime=runtime,
            job_id=job_id,
            model=submission.model,
            user_query=user_query,
            hook_manager=hook_manager,
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
                logging.warning("runtime.cancel(%s) failed: %s", session_id, exc)
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

        Returns the freshly-created :class:`IndexingJob`.
        """
        store: WikiStoreBase = runtime.wiki_store

        # 1. Verify the project exists.
        project = store.get_project(slug)
        if project is None:
            raise KeyError(f"Project not found: {slug}")
        logging.info("wiki refresh slug=%s", slug)

        # 2. Find the latest stored submission for this slug. A sidecar that
        # doesn't validate as a WizardSubmission is skipped as belt-and-
        # suspenders — restart recovery keeps its retry counter on its OWN
        # slug-keyed surface (store.{get,bump}_recovery_attempts), so our own
        # writes never pollute the submission sidecar; this guard only protects
        # against a hand-edited / legacy sidecar that wouldn't validate.
        from pydantic import ValidationError  # noqa: PLC0415

        submission: WizardSubmission | None = None
        jobs = sorted(
            store.list_jobs(slug=slug),
            key=lambda j: j.job_id,  # job_id is uuid hex — lexicographic ≈ creation order
            reverse=True,
        )
        for job in jobs:
            candidate = store.get_job_submission(job.job_id)
            if not candidate:
                continue
            try:
                submission = WizardSubmission.model_validate(candidate)
                break
            except ValidationError:
                continue

        # 3. Build a WizardSubmission — prefer stored; fall back to project fields.
        from mewbo_core.config import get_config, get_config_value  # noqa: PLC0415

        # The indexing default chain mirrors the wizard route: wiki default →
        # llm default (the QA chain lives in routes._resolve_qa_model).
        default_model = get_config_value(
            "wiki", "default_model", default=""
        ) or get_config_value("llm", "default_model", default="anthropic/claude-sonnet-4-6")
        if submission is None:
            # Derive minimal submission from the Project record.
            repo_url = f"https://github.com/{slug}" if project.source == "github" else slug
            submission = WizardSubmission.model_validate({
                "repoUrl": repo_url,
                "slug": slug,
                "platform": project.source,
                "depth": "comprehensive",
                "language": project.lang,
                "model": default_model,
                "filterMode": "exclude",
                "dirs": [],
                "files": [],
            })

        # 3b. Restore the durable credential the original onboard saved. The
        # reconstructed submission has token=None (the store never holds it);
        # without this, the clone fails on private hosts with
        # `fatal: could not read Username for '<host>'`. THIS line fixes the
        # token-less-refresh failure. SSH keys ride through on the same field
        # — the clone tool re-resolves the credential from the store by kind.
        cred = CredentialStore.load(store, slug)
        if cred is not None and cred.kind == "token":
            submission = submission.model_copy(update={"token": cred.value})

        # 3c. Guard against a stored model the proxy has since retired. A reindex
        # replays the stored submission's model verbatim, so a sticky model that
        # was valid when first chosen but later dropped from the proxy would
        # fast-fail the whole run on an invalid-model 400 (the SideStage
        # regression). Re-resolve to the wiki/llm default when it's no longer
        # offered; RetryStrategy's switch-on-invalid-model is the in-loop backstop
        # if the proxy can't be reached here.
        resolved_model = get_config().llm.resolve_available_model(
            submission.model, fallback=default_model
        )
        if resolved_model and resolved_model != submission.model:
            logging.warning(
                "wiki refresh slug=%s: stored model %r not offered by proxy; using %r",
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
# the fix.) ``spawn_agent``/``check_agents`` are injected for any depth-0 root
# regardless of strict scope — listed here for clarity (mirrors INDEXER_TOOLS)
# and so the approval callback admits them.
QA_TOOLS: list[str] = [
    "wiki_list_pages",       # cheap orientation only — titles, not content
    "wiki_emit_block",       # the answer renders ONLY through these
    "wiki_submit_insight",   # QA→memory flywheel (deposit a durable fact)
    "spawn_agent",           # fan out wiki-qa-probe retrieval probes
    "check_agents",          # collect probe findings
]

# The retrieval/traversal surface a ``wiki-qa-probe`` child may call. The probe's
# tool VISIBILITY comes from its own AgentDef (wiki-qa-probe.md ``tools:``), but
# the approval_callback is shared parent→child, so each of these must be admitted
# here too or the probe's calls fall through ASK → DENY and the probe is useless.
QA_PROBE_TOOLS: list[str] = [
    "wiki_search_pages",
    "wiki_read_page",
    "wiki_query_graph",
    "wiki_graph_neighbors",
    "wiki_code_search",
    "wiki_read_file",
    "wiki_grep",
    "wiki_list_files",
]

# Everything the QA run (root + its probes) is allowed to EXECUTE. ``steer_agent``
# is injected for the depth-0 root so it can cancel a stuck probe.
QA_APPROVED_TOOLS: frozenset[str] = frozenset(
    {*QA_TOOLS, *QA_PROBE_TOOLS, "steer_agent"}
)

# Hard cost backstop on the QA fan-out (#62). Probe count is prompt-guided
# (wiki-qa.md: "deploy as many as the question needs"), but an unbounded root
# spent ~1.1M tokens / 110 steps / 8+ probes on a 6-item question. This caps
# TOTAL tool steps across the root AND every wiki-qa-probe child (the hypervisor
# counts session-wide). Sized GENEROUS — a cost ceiling, not a tight cap: the
# happy path is ~13 steps, so 50 leaves wide headroom for a legitimately broad
# question while killing the 100+-step runaway.
QA_SESSION_STEP_BUDGET: int = 50


class WikiQaSession:
    """Static façade — all QA session state lives in the WikiStore."""

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
            summarySources=[],
            model=model,
            blocks=[],
            slug=slug,
        )
        store.save_qa(answer)

        session_tag = f"wiki:qa:{answer_id}"
        session_id = runtime.resolve_session(session_tag=session_tag)
        store.attach_qa_session(answer_id, session_id)

        # Advertise the ``wiki`` capability so the wiki-qa AgentDef stays
        # spawnable from inside this session (same gating as indexing).
        runtime.append_context_event(session_id, {"client_capabilities": ["wiki"]})

        # Emit meta immediately — before start_async — so the SSE generator
        # sees it as the first event regardless of how fast the agent runs.
        store.append_qa_event(answer_id, {
            "type": "meta",
            "answerId": answer_id,
            "model": model,
            "fromPageId": from_page_id,
        })

        playbook = _load_qa_playbook()
        # Strict tool scope + auto-approval for the closed wiki-qa surface.
        # Strict scope keeps the root LLM's *visible* tools to ``QA_TOOLS`` (no
        # auto-added shell/edit/activate_skill thrash). The approval callback
        # governs EXECUTION for the root AND its probe children (it is inherited
        # parent→child), so it admits the broader ``QA_APPROVED_TOOLS`` — the
        # root's tools plus every read-only tool a wiki-qa-probe may call.
        # Without it, ``wiki_emit_block`` (root) and every probe retrieval call
        # fall through ASK → DENY and nothing renders.
        def _approve_qa_tool(step: Any) -> bool:
            return getattr(step, "tool_id", None) in QA_APPROVED_TOOLS

        runtime.start_async(
            session_id=session_id,
            user_query=question,
            model_name=model,
            allowed_tools=QA_TOOLS,
            strict_tool_scope=True,
            approval_callback=_approve_qa_tool,
            skill_instructions=playbook,
            hook_manager=hook_manager,
            session_step_budget=QA_SESSION_STEP_BUDGET,
        )
        return answer

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
            logging.warning("marking %s cancelled on snapshot failed: %s", answer_id, exc)
        if session_id:
            try:
                runtime.cancel(session_id)
            except Exception as exc:  # pragma: no cover — best-effort
                logging.warning("runtime.cancel(%s) failed: %s", session_id, exc)
        return True


class WikiIndexingSessionEndHook:
    """Mark a non-terminal indexing job ``interrupted`` when its session ends.

    Defense-in-depth net for tool-internal infra failures (Gitea #56) and the
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
    """

    # Statuses that are already terminal — do not touch these.
    _TERMINAL: frozenset[str] = frozenset({"complete", "failed", "cancelled"})

    def __init__(self, runtime: Any) -> None:
        """Store a reference to the runtime for store access."""
        self._runtime = runtime

    def __call__(self, session_id: str, error: str | None) -> None:
        """The ``on_session_end`` callback."""
        try:
            store = self._runtime.wiki_store
            job_id = store.find_job_by_session(session_id)
            if not job_id:
                return
            job = store.get_job(job_id)
            if job is None or job.status in self._TERMINAL:
                return
            # Non-terminal job whose session ended — hand off to restart recovery.
            store.update_job(job_id, status="interrupted")
            logging.info(
                "wiki indexing session-end: marked job %s interrupted (was %s)",
                job_id, job.status,
            )
        except Exception:  # pragma: no cover — a session-end hook never blocks
            logging.warning("wiki indexing session-end hook failed", exc_info=True)


def _load_qa_playbook() -> str:
    """Read the wiki-qa.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-qa.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-qa.md not found at %s", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


class QaSessionEndHook:
    """Finalize a wiki-QA answer when its backing session ends.

    The QA counterpart to indexing's ``wiki_finalize`` tool: the hypervisor's
    terminal ``wiki_emit_block`` already closes the happy path (snapshot reconcile +
    ``complete``), but a run that *halts before* the sources block leaves the answer
    open. This atomic adapter (DI'd runtime) is registered on
    ``HookManager.on_session_end`` and is the net for that: it also stamps the one
    piece of provenance that needs the transport layer — the distinct ``models_used``
    that ran across the hypervisor + its probes, read from the session transcript
    (the down-layer ``QaFinalizer`` owns everything derivable from the QA log).

    Fires for EVERY session end; a non-QA session (``find_qa_by_session`` → ``None``)
    is a cheap no-op. ``error`` is non-None when the run halted → honest terminal state.
    """

    def __init__(self, runtime: Any) -> None:
        """Inject the runtime (wiki store + transcript reader)."""
        self._runtime = runtime

    def __call__(self, session_id: str, error: str | None) -> None:
        """The ``on_session_end`` callback — never raises (a failing hook must not block)."""
        from mewbo_graph.wiki.qa import QaFinalizer, QaMemoryDepositor  # noqa: PLC0415

        try:
            store = self._runtime.wiki_store
            answer_id = store.find_qa_by_session(session_id)
            if not answer_id:
                return
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
) -> str:
    """Create + start the wiki-indexer session for *job_id*; return the session id.

    The single chokepoint for the "resolve a ``wiki:job:<id>`` session, advertise
    the ``wiki`` capability, prepare the clone dir, and ``start_async`` the indexer
    with INDEXER_TOOLS + the playbook" sequence — shared by :meth:`start` and the
    checkpoint-aware :class:`WikiResume.resume` so the capability advertisement,
    tool allowlist, and approval callback can never drift between the two paths
    (advertising the wrong capability is the classic "stuck after scan" bug).

    Resume guidance (``ResumePlan.summary()``) rides the *user_query* task
    description (see ``_render_resume_query``), not a separate system-prompt slot.
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
        allowed_tools=INDEXER_TOOLS,
        skill_instructions=skill_instructions,
        cwd=cwd,
        hook_manager=hook_manager,
        approval_callback=auto_approve,
    )
    return session_id


def _load_indexer_playbook() -> str:
    """Read the wiki-indexer.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-indexer.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-indexer.md not found at %s", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


def _render_resume_query(store: WikiStoreBase, job: IndexingJob, plan: Any) -> str:
    """Render the user-query string for a checkpoint-aware RESUME of *job*.

    Reconstructs the submission contract from the stored submission (token-less;
    the clone tool re-resolves the credential from the durable store) and pins the
    clone to the job's recorded ``commit_sha`` so the reused graph stays consistent.
    Falls back to the project/job fields when no submission sidecar validates.
    """
    submission: WizardSubmission | None = None
    raw = store.get_job_submission(job.job_id)
    if raw:
        try:
            submission = WizardSubmission.model_validate(raw)
        except Exception:
            submission = None

    repo_url = submission.repo_url if submission else None
    platform = submission.platform if submission else job.platform
    depth = submission.depth if submission else "comprehensive"
    language = submission.language if submission else "en"
    filter_mode = submission.filter_mode if submission else "exclude"
    dirs = submission.dirs if submission else []
    files = submission.files if submission else []

    ref_note = (
        f"  ref: {job.commit_sha} (RESUME — clone at this recorded commit, NOT latest HEAD)\n"
        if job.commit_sha
        else ""
    )
    return (
        "RESUME an interrupted DeepWiki-style index of this repository.\n\n"
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
        + ref_note
        + "  auth: <token resolved server-side — call wiki_clone_repo with token=null>\n"
        + "\n"
        + plan.summary()
        + "\n\nProceed per the wiki-indexer playbook, honouring the RESUME guidance above."
    )


def _render_user_query(submission: WizardSubmission) -> str:
    """Render the user-query string the indexer agent receives."""
    token_note = (
        "  auth: <token stashed server-side — call wiki_clone_repo with token=null>\n"
        if submission.token
        else "  auth: <none — public repo>\n"
    )
    return (
        "Index this repository as a DeepWiki-style site.\n\n"
        "SUBMISSION:\n"
        f"  repoUrl: {submission.repo_url}\n"
        f"  slug: {submission.slug}\n"
        f"  platform: {submission.platform}\n"
        f"  depth: {submission.depth}\n"
        f"  language: {submission.language}\n"
        f"  model: {submission.model}\n"
        f"  filterMode: {submission.filter_mode}\n"
        f"  dirs: {submission.dirs}\n"
        f"  files: {submission.files}\n"
        + token_note
        + "\nProceed per the wiki-indexer playbook."
    )
