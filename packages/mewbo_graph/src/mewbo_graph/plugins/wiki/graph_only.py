"""``GraphOnlyIndexer`` — deterministic, zero-LLM repository indexing.

Developer mode unlocks a DETERMINISTIC index path: ``clone → scan → graph →
finalize``, SKIPPING the LLM-driven phases (``enrich``/``plan``/``pages``). The
result is a project with a fully populated AST code graph (still visualisable via
``GET /v1/wiki/projects/<slug>/graph``) but ZERO documentation pages; the project
record is stamped ``graph_only=True`` so the doc-content read seam raises
:class:`~mewbo_graph.wiki.errors.DocumentationUnavailableError`.

This is an atomic class: it holds the per-run state (job ctx + submission) and
its behaviour is methods over that state. It REUSES the existing phase tools'
cores — the ``clone``/``scan`` head it inherits from
:class:`~mewbo_graph.plugins.wiki._jobless.JoblessIndexRunner`, the tree-sitter
parse+persist+embed core (:func:`build_graph.build_graph_core`), and the
finalize helpers — so there is no duplicated git/scan/tree-sitter logic. It
emits the SAME ``phase``/``log``/``queued``/``complete`` events the agent path
does, so the job-status SSE stream + console progress keep working unchanged.

It runs with NO Mewbo session and NO LLM agent: the API calls
:meth:`GraphOnlyIndexer.run` on a background thread (mirroring ``start_async``),
not by spawning a ``wiki-indexer`` playbook.
"""
from __future__ import annotations

import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING

from mewbo_core.common import get_logger

from mewbo_graph.plugins.wiki._ctx import (
    ProgressReporter,
    build_jobless_ctx,
    emit_log,
    emit_phase,
)
from mewbo_graph.plugins.wiki._jobless import JoblessIndexRunner, JoblessPhaseError
from mewbo_graph.plugins.wiki.build_graph import build_graph_core
from mewbo_graph.plugins.wiki.clone import _git_rev_parse
from mewbo_graph.plugins.wiki.finalize import (
    _detect_grounder,
    _graph_is_populated,
    _host_from_url,
    _resolve_graph_resolution,
    _resolve_index_fingerprint,
    _resolve_project_desc,
    _supersede_stale_jobs,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mewbo_graph.plugins.wiki._ctx import WikiJobCtx
    from mewbo_graph.wiki.store import WikiStoreBase

logging = get_logger(name="mewbo_graph.plugins.wiki.graph_only")

# Synthetic landing page id for a graph-only project. There are no pages, but
# the Project record + FE deep-links expect a non-empty ``landing_page_id``; the
# graph view is the canonical entry point, so we point at it. The FE's graph-only
# empty state never tries to READ this page (the doc-read seam would raise).
_GRAPH_ONLY_LANDING_ID = "graph"


class GraphOnlyIndexer(JoblessIndexRunner):
    """Deterministic clone→scan→graph→finalize indexing (no LLM).

    Construct with the durable handles a run needs (``ctx`` + the ``submission``
    contract), then call :meth:`run`. All state lives in the wiki store via the
    same event/snapshot writes the agent path uses. The ``clone``/``scan`` head,
    the cooperative cancel poll and the never-raise driver are inherited; what
    is specific to developer mode is the tail — build the AST graph, then
    finalize a paged-less project.
    """

    _RUN_NOUN = "graph-only index"
    _CLONE_NOTE = "graph-only"
    _FAILURE_NOUN = "graph-only indexing"

    def _phases(self) -> tuple[Callable[[], None], ...]:
        """Clone → scan → graph → finalize; ``enrich``/``plan``/``pages`` skipped."""
        return (self._clone, self._scan, self._build_graph, self._finalize)

    def _rev_parse(self, clone_dir: Path, args: list[str]) -> str | None:
        """Resolve ``_git_rev_parse`` through THIS module, per the base's note.

        Keeping the lookup here means a test patching ``graph_only._git_rev_parse``
        still intercepts the git metadata read after the clone phase moved to the
        base class — the alternative silently shells out to real git against a
        fixture checkout that is not a repository.
        """
        return _git_rev_parse(clone_dir, args)

    # ── phases ──────────────────────────────────────────────────────────

    def _build_graph(self) -> None:
        """Parse + persist + embed the AST graph via the shared deterministic core.

        Phase transitions are owned here (the core never emits them): ``graph``
        before, then straight to ``finalize`` — the agent path's ``enrich`` window
        is skipped entirely (no entity fan-out, no LLM).
        """
        ctx = self._ctx
        if not ctx.clone_dir.exists():
            raise JoblessPhaseError("internal", f"clone dir missing: {ctx.clone_dir}")
        emit_phase(ctx, "graph")
        ProgressReporter(ctx).skip_group(
            "enrich", note="not run by graph-only index"
        )
        ProgressReporter(ctx).skip_group(
            "plan", note="not run by graph-only index"
        )
        ProgressReporter(ctx).skip_group(
            "pages", note="not run by graph-only index"
        )
        result = build_graph_core(ctx)
        ProgressReporter(ctx).finish_group("clone")
        ProgressReporter(ctx).finish_group("scan")
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
            raise JoblessPhaseError(
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
        # (a project first indexed graph-only has no pages to drop).
        dropped = ctx.store.prune_pages(ctx.slug, keep=())
        if dropped:
            emit_log(ctx, f"Dropped {dropped} page(s) — this project is now graph-only")

        job = ctx.store.get_job(ctx.job_id)
        branch = (job.branch if job else None) or None
        commit_sha = (job.commit_sha if job else None) or None
        commit_short = commit_sha[:7] if commit_sha else None
        maintainer_edited = _detect_grounder(ctx.clone_dir)
        # Shared read-only copy seam — the same one ``wiki_finalize`` uses, so
        # a graph-only project gets a fingerprint too (it is exempt from the
        # resume-skip gap this seam guards against, since graph-only runs are
        # never checkpoint-resumed, but it must not be exempt from HAVING one).
        fingerprint = _resolve_index_fingerprint(ctx)
        # Same seam, same reason: there are TWO Project write sites and a
        # read-preserve copy wired into only one of them means a graph-only
        # project silently reports "resolver outcome unknown" while its job
        # record holds the real answer.
        resolution = _resolve_graph_resolution(ctx)

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
            fingerprint=fingerprint,
            resolution=resolution,
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
        ProgressReporter(ctx).finish_group("finalize")
        emit_log(ctx, "Graph-only wiki ready: AST graph built, no documentation pages")
        ctx.store.append_job_event(ctx.job_id, {
            "type": "complete",
            "landingPageId": _GRAPH_ONLY_LANDING_ID,
            "pageCount": 0,
        })

        # Defense-in-depth, matching the other two terminal tools: the
        # explicit skip/finish_group calls above cover the phases this shape
        # knows it bypasses, but ``build_graph_core`` (shared with the
        # agent-driven path) is code this runner does not own — a future gap
        # in it should read as "settled by finalize", not as a permanently
        # open step on a completed graph-only job.
        # ``settle`` emits its own timeline warning for an unreported step, so
        # no call site repeats it: four terminal callers each remembering to
        # log the same thing is the second-writer shape this ledger removed.
        ProgressReporter(ctx).settle()


def build_graph_only_ctx(
    *, job_id: str, slug: str, store: WikiStoreBase
) -> WikiJobCtx:
    """Build the :class:`WikiJobCtx` a :class:`GraphOnlyIndexer` run operates over.

    A thin alias over :func:`~mewbo_graph.plugins.wiki._ctx.build_jobless_ctx`,
    which every sessionless runner shares. The name stays because the API's
    indexing + resume paths import it; the body moved so a second runner did not
    have to copy it.
    """
    return build_jobless_ctx(job_id=job_id, slug=slug, store=store)


__all__ = ["GraphOnlyIndexer", "build_graph_only_ctx"]
