"""``GraphOnlyIndexer`` — deterministic, zero-LLM repository onboarding.

Developer mode unlocks a DETERMINISTIC index path: ``clone → scan → graph →
finalize``, SKIPPING the LLM-driven phases (``enrich``/``plan``/``pages``). The
result is a project with a fully populated AST code graph (still visualisable via
``GET /v1/wiki/projects/<slug>/graph``) but ZERO documentation pages; the project
record is stamped ``graph_only=True`` so the doc-content read seam raises
:class:`~mewbo_graph.wiki.errors.DocumentationUnavailableError`.

This is an atomic class: it holds the per-run state (job ctx + submission) and
its behaviour is methods over that state. It REUSES the existing phase tools'
cores — git clone helpers (:mod:`clone`), the scan walk (:func:`scan._collect_files`),
the tree-sitter parse+persist+embed core (:func:`build_graph.build_graph_core`),
and the finalize helpers — so there is no duplicated git/scan/tree-sitter logic.
It emits the SAME ``phase``/``log``/``queued``/``complete`` events the agent path
does, so the job-status SSE stream + console progress keep working unchanged.

It runs with NO Mewbo session and NO LLM agent: the API calls
:meth:`GraphOnlyIndexer.run` on a background thread (mirroring ``start_async``),
not by spawning a ``wiki-indexer`` playbook.
"""
from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger

from mewbo_graph.plugins.wiki._ctx import WikiJobCtx, emit_log, emit_phase
from mewbo_graph.plugins.wiki.build_graph import build_graph_core
from mewbo_graph.plugins.wiki.clone import (
    _git_rev_parse,
    clone_with_fallback,
)
from mewbo_graph.plugins.wiki.finalize import (
    _detect_grounder,
    _graph_is_populated,
    _host_from_url,
    _resolve_project_desc,
    _supersede_stale_jobs,
)
from mewbo_graph.plugins.wiki.scan import WikiScanArgs, _collect_files

if TYPE_CHECKING:
    from pathlib import Path

    from mewbo_graph.wiki.store import WikiStoreBase
    from mewbo_graph.wiki.types import WizardSubmission

logging = get_logger(name="mewbo_graph.plugins.wiki.graph_only")

# Synthetic landing page id for a graph-only project. There are no pages, but
# the Project record + FE deep-links expect a non-empty ``landing_page_id``; the
# graph view is the canonical entry point, so we point at it. The FE's graph-only
# empty state never tries to READ this page (the doc-read seam would raise).
_GRAPH_ONLY_LANDING_ID = "graph"


class GraphOnlyIndexer:
    """Deterministic clone→scan→graph→finalize onboarding (no LLM).

    Construct with the durable handles a run needs (``ctx`` + the ``submission``
    contract), then call :meth:`run`. All state lives in the wiki store via the
    same event/snapshot writes the agent path uses.
    """

    def __init__(self, ctx: WikiJobCtx, submission: WizardSubmission) -> None:
        """Bind the job ctx and the submission contract for this run."""
        self._ctx = ctx
        self._submission = submission

    # ── public entry point ──────────────────────────────────────────────

    def run(self) -> None:
        """Drive the full deterministic pipeline; mark the job failed on error.

        Never raises into the caller (mirrors a session that ends cleanly): a
        phase failure is recorded on the job snapshot + event log so the SSE
        stream + landing card show an honest terminal state, exactly as the
        agent path's error handling does.
        """
        ctx = self._ctx
        try:
            # Cooperative cancellation: ``WikiIndexingJob.cancel`` marks the job
            # record ``cancelled`` from the request thread, but this daemon thread
            # owns the terminal write. Re-read the job status BEFORE each phase and
            # bail WITHOUT overwriting ``cancelled`` — otherwise the run would clobber
            # the user's cancel with a later ``complete``/``failed``.
            for phase in (self._clone, self._scan, self._build_graph, self._finalize):
                if self._cancelled():
                    emit_log(ctx, "Graph-only index cancelled — stopping")
                    return
                phase()
        except _GraphOnlyError as exc:
            if not self._cancelled():
                self._fail(exc.code, str(exc))
        except Exception as exc:  # noqa: BLE001 — never leak into the worker thread
            logging.warning("graph-only index failed for {}: {}", ctx.slug, exc)
            if not self._cancelled():
                self._fail("internal", f"graph-only indexing failed: {exc}")

    # ── phases ──────────────────────────────────────────────────────────

    def _clone(self) -> None:
        """Shallow-clone the repo into ``ctx.clone_dir`` and stamp git metadata.

        Delegates to the SAME :func:`clone_with_fallback` the ``wiki_clone_repo``
        tool uses (credential chain: submission token → durable repo/host store →
        ambient git credential → anonymous; per-attempt dir reset; helper-disable
        + prompt-off env; secret redaction) — no duplicated resolution/SSH logic.
        """
        ctx = self._ctx
        sub = self._submission
        url = sub.repo_url or ""
        if not url:
            raise _GraphOnlyError("validation", "graph-only index requires a repo URL")

        clone_dir = ctx.clone_dir
        emit_phase(ctx, "clone")
        emit_log(ctx, f"Cloning {url} (graph-only)…")

        outcome = clone_with_fallback(
            url,
            clone_dir,
            ref=sub.ref,
            store=ctx.store,
            slug=ctx.slug,
            arg_token=sub.token,
            on_log=lambda text, *, level="info": emit_log(ctx, text, level=level),
        )
        if not outcome.ok:
            raise _GraphOnlyError("repo_access", outcome.stderr)

        total = self._count_files(clone_dir)
        head = _git_rev_parse(clone_dir, ["HEAD"]) or ""
        branch = _git_rev_parse(clone_dir, ["--abbrev-ref", "HEAD"]) or ""
        if not branch or branch == "HEAD":
            branch = ""

        ctx.store.update_job(
            ctx.job_id,
            status="scanning",
            total_count=total,
            branch=branch or None,
            commit_sha=head or None,
        )
        ctx.store.append_job_event(ctx.job_id, {
            "type": "queued",
            "jobId": ctx.job_id,
            "slug": ctx.slug,
            "totalCount": total,
        })
        emit_log(ctx, f"Cloned {total} files into {clone_dir.name}")

    def _scan(self) -> None:
        """Walk the clone tree applying the submission's filters; emit scan events.

        Reuses :func:`scan._collect_files` so the always-exclude + glob-filter
        logic is shared with the tool. Emits per-file ``scanning``/``scanned``
        events + ``scanned_count`` so the scan-phase sub-progress bar fills.
        """
        ctx = self._ctx
        clone_dir = ctx.clone_dir
        if not clone_dir.exists():
            raise _GraphOnlyError("internal", f"clone dir missing: {clone_dir}")

        emit_phase(ctx, "scan")
        args = WikiScanArgs(
            filter_mode=self._submission.filter_mode,
            dirs=list(self._submission.dirs),
            files=list(self._submission.files),
        )
        files = _collect_files(clone_dir, args)
        total = len(files)
        emit_log(ctx, f"Scanning {total} files in {clone_dir.name}…")
        for idx, rel in enumerate(files):
            file_str = str(rel)
            ctx.store.append_job_event(ctx.job_id, {
                "type": "scanning", "file": file_str, "index": idx, "totalCount": total,
            })
            ctx.store.append_job_event(ctx.job_id, {
                "type": "scanned", "file": file_str, "index": idx, "totalCount": total,
            })
            ctx.store.update_job(ctx.job_id, current_file=file_str, scanned_count=idx + 1)
        emit_log(ctx, f"Scanned {total} files")

    def _build_graph(self) -> None:
        """Parse + persist + embed the AST graph via the shared deterministic core.

        Phase transitions are owned here (the core never emits them): ``graph``
        before, then straight to ``finalize`` — the agent path's ``enrich`` window
        is skipped entirely (no entity fan-out, no LLM).
        """
        ctx = self._ctx
        if not ctx.clone_dir.exists():
            raise _GraphOnlyError("internal", f"clone dir missing: {ctx.clone_dir}")
        emit_phase(ctx, "graph")
        result = build_graph_core(ctx)
        emit_log(
            ctx,
            f"Graph built (graph-only): {result.get('nodeCount', 0)} nodes, "
            f"{result.get('edgeCount', 0)} edges — skipping enrich/plan/pages",
        )

    def _finalize(self) -> None:
        """Persist a ``graph_only`` Project (zero pages) + emit the complete event.

        Mirrors ``wiki_finalize`` (identity from the submission, git snapshot off
        the job, description fetch, completion-correctness graph gate, supersede),
        but with NO landing-page validation (there are no pages) and stamps
        ``graph_only=True`` + a synthetic landing id pointing at the graph view.
        """
        from mewbo_graph.wiki.types import Project  # noqa: PLC0415

        ctx = self._ctx
        sub = self._submission
        repo_url = sub.repo_url or ""
        source = sub.platform
        lang = sub.language or "en"

        # Completion correctness (GraphRAG ordering law): a graph-only run whose
        # whole point IS the graph must refuse to "complete" with an empty graph.
        if not _graph_is_populated(ctx):
            raise _GraphOnlyError(
                "validation",
                "cannot finalize: knowledge graph is empty — the graph build "
                "produced no nodes",
            )

        # Shared read-preserve seam (user override → platform fetch → previous
        # record) — the same one ``wiki_finalize`` uses, so an edited description
        # survives a reindex on BOTH paths.
        desc = _resolve_project_desc(ctx.store, ctx.slug, repo_url=repo_url, platform=source)

        # A graph-only project has ZERO pages by definition. When a slug is FLIPPED
        # to graph-only, the pages from its prior
        # documented index would otherwise survive in the store as orphans: the
        # Project is stamped ``graph_only=True``, so the doc-read seam
        # (``get_page`` → ``DocumentationUnavailableError``) makes every one of them
        # unreachable — dead rows that a later flip BACK would then mix with freshly
        # generated ones (the slug-drift duplication ``prune_pages`` exists to kill).
        # ``wiki_finalize`` prunes to its committed plan for exactly this reason; the
        # graph-only plan is empty, so prune to nothing. A no-op on the common path
        # (a project onboarded graph-only from the start has no pages to drop).
        dropped = ctx.store.prune_pages(ctx.slug, keep=())
        if dropped:
            emit_log(ctx, f"Dropped {dropped} page(s) — this project is now graph-only")

        job = ctx.store.get_job(ctx.job_id)
        branch = (job.branch if job else None) or None
        commit_sha = (job.commit_sha if job else None) or None
        commit_short = commit_sha[:7] if commit_sha else None
        maintainer_edited = _detect_grounder(ctx.clone_dir)

        indexed_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        project = Project(
            slug=ctx.slug,
            source=source,
            lang=lang,
            indexedAt=indexed_at,
            pages=0,
            primary=False,
            desc=desc,
            landingPageId=_GRAPH_ONLY_LANDING_ID,
            repoUrl=repo_url or None,
            host=_host_from_url(repo_url),
            branch=branch,
            commitSha=commit_sha,
            commitShort=commit_short,
            maintainerEdited=maintainer_edited,
            graphOnly=True,
        )
        ctx.store.create_project(project)

        ctx.store.update_job(
            ctx.job_id,
            status="complete",
            landing_page_id=_GRAPH_ONLY_LANDING_ID,
            current_file=None,
        )
        _supersede_stale_jobs(ctx)

        # Reap prior-commit graph artifacts, exactly as ``wiki_finalize`` does:
        # a graph-only re-index at a new commit must not union with the old one.
        if commit_sha:
            try:
                ctx.store.supersede_graph_artifacts(ctx.slug, keep_commit_sha=commit_sha)
            except Exception as exc:  # pragma: no cover — best-effort cleanup
                logging.info("graph-only finalize: supersede failed ({})", exc)

        emit_phase(ctx, "finalize")
        emit_log(ctx, "Graph-only wiki ready: AST graph built, no documentation pages")
        ctx.store.append_job_event(ctx.job_id, {
            "type": "complete",
            "landingPageId": _GRAPH_ONLY_LANDING_ID,
            "pageCount": 0,
        })

    # ── helpers ─────────────────────────────────────────────────────────

    def _cancelled(self) -> bool:
        """True iff the job was cancelled out-of-band (re-read from the store).

        ``WikiIndexingJob.cancel`` sets status ``cancelled`` from the request
        thread; this daemon thread polls it at phase boundaries to stop early
        without clobbering the cancel. Best-effort — a store hiccup reads as
        not-cancelled (the run proceeds, no worse than today).
        """
        try:
            job = self._ctx.store.get_job(self._ctx.job_id)
        except Exception:  # pragma: no cover — best-effort
            return False
        return job is not None and job.status == "cancelled"

    @staticmethod
    def _count_files(clone_dir: Path) -> int:
        """Count files in *clone_dir*, skipping ``.git`` internals."""
        return sum(
            1 for p in clone_dir.rglob("*") if p.is_file() and ".git" not in p.parts
        )

    def _fail(self, code: str, message: str) -> None:
        """Record a terminal failure on the job snapshot + event log (best-effort)."""
        ctx = self._ctx
        try:
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error", "error": {"code": code, "message": message},
            })
            ctx.store.update_job(ctx.job_id, status="failed", current_file=None)
        except Exception:  # pragma: no cover — best-effort honesty
            logging.warning("graph-only fail-record for {} failed", ctx.slug, exc_info=True)


class _GraphOnlyError(Exception):
    """Internal carrier of a (code, message) phase failure within the indexer."""

    def __init__(self, code: str, message: str) -> None:
        """Store the wiki-error code alongside the message."""
        self.code = code
        super().__init__(message)


def build_graph_only_ctx(
    *, job_id: str, slug: str, store: WikiStoreBase
) -> WikiJobCtx:
    """Build the :class:`WikiJobCtx` a :class:`GraphOnlyIndexer` run operates over.

    The deterministic path has no Mewbo session, so ``session_id`` is empty and
    ``resume_plan`` is ``None`` (graph-only runs are never checkpoint-resumed —
    they are cheap enough to restart whole). ``clone_dir`` resolves via the same
    ``_clone_dir_for`` rule the agent path uses, so the clone lands where the
    graph view + source endpoint expect it.
    """
    from mewbo_graph.plugins.wiki._ctx import _clone_dir_for  # noqa: PLC0415

    return WikiJobCtx(
        job_id=job_id,
        slug=slug,
        session_id="",
        clone_dir=_clone_dir_for(job_id),
        store=store,
        resume_plan=None,
    )


__all__ = ["GraphOnlyIndexer", "build_graph_only_ctx"]
