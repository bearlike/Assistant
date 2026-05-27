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
from mewbo_graph.wiki.store import WikiStoreBase
from mewbo_graph.wiki.tokens import CloneTokenCache
from mewbo_graph.wiki.types import IndexingJob, QaAnswer, WizardSubmission

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

        # Resolve/create the Mewbo session.
        session_tag = f"wiki:job:{job_id}"
        session_id = runtime.resolve_session(session_tag=session_tag)
        store.attach_job_session(job_id, session_id)

        # Advertise the ``wiki`` capability so the agent_registry exposes
        # wiki-* AgentDefs (wiki-indexer, wiki-page-writer, wiki-qa) to
        # spawn_agent lookups. Without this, the indexer's per-page
        # spawns return "Unknown agent type 'wiki-page-writer'".
        runtime.append_context_event(session_id, {"client_capabilities": ["wiki"]})

        # Prepare clone dir.
        clone_root = os.environ.get("MEWBO_WIKI_CLONE_ROOT") or "/tmp/mewbo/wiki/clones"
        cwd = str(Path(clone_root) / job_id)
        Path(cwd).mkdir(parents=True, exist_ok=True)

        # Load the indexer playbook (system-prompt extension).
        skill_instructions = _load_indexer_playbook()

        # Build user query that carries the submission contract.
        user_query = _render_user_query(submission)

        # Stash the token for wiki_clone_repo to read — keeps it out of the
        # LLM transcript while still letting the tool authenticate the clone.
        if submission.token:
            CloneTokenCache.store(job_id, submission.token)
        runtime.start_async(
            session_id=session_id,
            user_query=user_query,
            model_name=submission.model,
            allowed_tools=INDEXER_TOOLS,
            skill_instructions=skill_instructions,
            cwd=cwd,
            hook_manager=hook_manager,
            approval_callback=auto_approve,
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

        # 2. Find the latest stored submission for this slug.
        sub_dict: dict[str, Any] | None = None
        jobs = sorted(
            store.list_jobs(slug=slug),
            key=lambda j: j.job_id,  # job_id is uuid hex — lexicographic ≈ creation order
            reverse=True,
        )
        for job in jobs:
            candidate = store.get_job_submission(job.job_id)
            if candidate:
                sub_dict = candidate
                break

        # 3. Build a WizardSubmission — prefer stored; fall back to project fields.
        from mewbo_core.config import get_config_value  # noqa: PLC0415

        default_model = get_config_value(
            "llm", "default_model", default="anthropic/claude-sonnet-4-6"
        )
        if sub_dict:
            # Restore camelCase fields that the store persists.
            submission = WizardSubmission.model_validate(sub_dict)
        else:
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

# Tools the wiki-qa agent is allowed to call. Mirrors wiki-qa.md frontmatter.
# Agentic surface: catalog → page-read → graph → source-files → emit.
# ``wiki_code_search`` was previously listed as ``code_search`` — that name
# never matched a registered tool_id, so the LLM saw it in the catalog but
# the tool didn't exist. Fixed to the canonical id.
QA_TOOLS: list[str] = [
    "wiki_list_pages",
    "wiki_search_pages",
    "wiki_read_page",
    "wiki_query_graph",
    "wiki_graph_neighbors",
    "wiki_code_search",
    "wiki_read_file",
    "wiki_grep",
    "wiki_list_files",
    "wiki_emit_block",
    "wiki_submit_insight",   # QA→memory flywheel (deposit a durable fact)
]


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
        # Strict tool scope + auto-approval for the closed wiki-qa tool set.
        # Without strict scoping the orchestrator auto-adds every built-in
        # (shell, edit, spawn_agent, activate_skill, ...) on top of
        # ``allowed_tools``, and the LLM thrashes through them — each round
        # trip is ~1.5-2 s on a fast model. Without an approval callback
        # ``wiki_emit_block`` falls through ASK → DENY and the agent can't
        # render the answer.
        qa_tool_set = frozenset(QA_TOOLS)

        def _approve_qa_tool(step: Any) -> bool:
            return getattr(step, "tool_id", None) in qa_tool_set

        runtime.start_async(
            session_id=session_id,
            user_query=question,
            model_name=model,
            allowed_tools=QA_TOOLS,
            strict_tool_scope=True,
            approval_callback=_approve_qa_tool,
            skill_instructions=playbook,
            hook_manager=hook_manager,
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
        if session_id:
            try:
                runtime.cancel(session_id)
            except Exception as exc:  # pragma: no cover — best-effort
                logging.warning("runtime.cancel(%s) failed: %s", session_id, exc)
        return True


def _load_qa_playbook() -> str:
    """Read the wiki-qa.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-qa.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-qa.md not found at %s", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


# ---------------------------------------------------------------------------
# WikiIndexingJob helpers
# ---------------------------------------------------------------------------


def _load_indexer_playbook() -> str:
    """Read the wiki-indexer.md AgentDef body. Falls back to empty string if missing."""
    agent_md = _WIKI_AGENTS_DIR / "wiki-indexer.md"
    if not agent_md.exists():  # pragma: no cover
        logging.warning("wiki-indexer.md not found at %s", agent_md)
        return ""
    agent_def = parse_agent_file(agent_md, source="plugin:wiki")
    return agent_def.body if agent_def else ""


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
