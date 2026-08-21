"""``wiki_finalize`` SessionTool — persist Project record, emit complete event."""
from __future__ import annotations

import datetime
import shutil
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import (
    ProgressReporter,
    _clone_dir_for,
    emit_log,
    emit_phase,
)
from mewbo_graph.plugins.wiki._platform_api import (
    api_get_json_with_chain,
    github_api_base,
)
from mewbo_graph.plugins.wiki.clone import (  # noqa: F401 — _resolve_runtime is the per-module test seam
    _is_private_host,
    _resolve_runtime,
)
from mewbo_graph.plugins.wiki.grounder import _DEFAULT_GROUNDER_PATHS
from mewbo_graph.plugins.wiki.mermaid import MermaidValidator
from mewbo_graph.plugins.wiki.step_plans import (
    PHASE_STEPS,
    planned_steps_for_slug,
)
from mewbo_graph.wiki.credentials import CredentialScope

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

    from mewbo_graph.wiki.types import GraphResolution, IndexFingerprint

logging = get_logger(name="mewbo_graph.plugins.wiki.finalize")


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiFinalizeArgs(BaseModel):
    """Arguments for ``wiki_finalize``."""

    model_config = ConfigDict(extra="forbid")

    landingPageId: str = Field(  # noqa: N815
        ...,
        description="The slug-style page id to land on after indexing.",
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiFinalizeTool(WikiSessionTool):
    """SessionTool: finalize indexing — persist Project record, emit complete event.

    Terminates the run on success (mirrors ``EmitStructuredResponseTool``):
    ``should_terminate_run()`` returns ``True`` the step after a successful
    ``handle()``, so the loop breaks immediately — no extra post-finalize LLM
    turn, no wasted tokens, and the terminal events are emitted while the
    session is still coherent.
    """

    tool_id = "wiki_finalize"
    args_cls = WikiFinalizeArgs
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiFinalizeArgs, name="wiki_finalize")

    def __init__(self, session_id: str, event_logger: Any = None) -> None:
        """Initialise with a pending-terminate flag."""
        super().__init__(session_id, event_logger)
        self._terminate_run_pending: bool = False

    def should_terminate_run(self) -> bool:
        """Return True once after a successful finalize; resets the flag."""
        if self._terminate_run_pending:
            self._terminate_run_pending = False
            return True
        return False

    def terminal_reason(self) -> str:
        """Return the done_reason emitted when the loop terminates."""
        return "completed"

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_finalize`` tool call."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiFinalizeArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        progress = ProgressReporter(ctx)
        progress.declare(planned_steps_for_slug(ctx.store, ctx.slug, "finalize"))

        # 3. Verify landingPageId exists in the persisted pages, then drop
        # stale pages from prior runs that aren't in this run's plan. Without
        # this, re-indexing accumulates slug-drifted duplicates ("Auth and
        # Pairing" + "Authentication and Session Security" + ...) because
        # each LLM run picks slightly different page ids for the same topics.
        # The committed plan (from wiki_commit_plan) is the source of truth
        # for what should remain after this run.
        with progress.step("finalize.reconcile_pages"):
            plan = ctx.store.get_job_plan(ctx.job_id) or []
            plan_ids: set[str] = {
                entry.get("id", "") for entry in plan if entry.get("id")
            }
            if plan_ids:
                keep = plan_ids | {args.landingPageId}
                dropped = ctx.store.prune_pages(ctx.slug, keep)
                if dropped:
                    emit_log(ctx, f"Dropped {dropped} stale page(s) not in this run's plan")
            pages = ctx.store.list_pages(ctx.slug)
            page_count = len(pages)

        # 3b. Outcome assertion: an index that wrote NO pages produced no wiki.
        # ``page_count`` was computed, persisted onto the Project and logged as
        # "Wiki ready: N pages" but never actually checked, so a run that reached
        # here having never called wiki_submit_page still recorded itself
        # complete. Zero pages is a FAILURE — mark the job terminally failed so it
        # surfaces as a real error instead of a 0-page success.
        if page_count == 0:
            err = (
                "cannot finalize: no pages were written — the index produced no "
                "documentation"
            )
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error",
                "error": {"code": "validation", "message": err},
            })
            ctx.store.update_job(ctx.job_id, status="failed", current_file=None)
            return _err_result("validation", err)

        page_ids = {p.id for p in pages}
        if args.landingPageId not in page_ids:
            return _err_result(
                "validation",
                f"landingPageId '{args.landingPageId}' not found in submitted pages "
                f"({sorted(page_ids) or 'none'})",
            )

        # 3c. Mermaid gate. A diagram that fails to parse renders as an error card,
        # so the page ships visibly broken while the index reports success — the
        # pipeline had no step that noticed.
        #
        # This refuses the FINALIZE, never the pages. Every page is already
        # persisted by ``wiki_submit_page`` and stays persisted: the expensive
        # work (clone, scan, graph, enrich, generation) is never redone, and the
        # refusal names the page, block and line so a repair is scoped to the few
        # broken diagrams instead of the whole wiki. Deliberately NOT a
        # ``update_job(status="failed")`` like the zero-pages and empty-graph
        # gates above — this state is repairable in-run, and the run must stay
        # alive for the model to repair it. Returning without setting
        # ``_terminate_run_pending`` is what keeps it alive: the loop reads the
        # refusal, fixes the diagrams and calls finalize again.
        #
        # Gating per BLOCK, not per page, is load-bearing: every affected page
        # observed carried exactly one bad diagram among otherwise-good ones, so
        # rejecting whole pages would discard sound work for no reason.
        with progress.step("finalize.validate_diagrams", total=page_count) as step:
            validator = MermaidValidator()
            rejection = None
            for page in step.count(pages, total=page_count):
                rejection = validator.review(((page.id, page.body),))
                if rejection is not None:
                    break
            if rejection is not None:
                emit_log(ctx, rejection.error.message)
                return MockSpeaker(content=str(rejection.model_dump()))

        # 4. Resolve identity from the persisted submission. The wizard
        # is the canonical source: it carries the explicit platform, the
        # full repo URL (host + path), and the language. We do NOT do
        # any URL-host → platform guessing here — that breaks for any
        # enterprise/self-hosted instance the heuristic doesn't know.
        with progress.step("finalize.resolve_metadata"):
            submission = _load_submission(ctx)
            if not submission:
                return _err_result(
                    "internal",
                    "wiki submission is missing — cannot finalize without canonical identity",
                )
            repo_url = submission.get("repoUrl") or ""
            source = submission.get("platform") or ""
            lang = submission.get("language") or "en"
            if not source:
                return _err_result(
                    "validation",
                    "submission.platform is required",
                )
            host = _host_from_url(repo_url)

            # The description a reindex persists: a user's edited description wins,
            # else the platform's public API, else whatever the previous successful run
            # wrote (a token-less refresh against a private host fetches ""). All three
            # tiers live in ``_resolve_project_desc`` — the read-preserve seam this and
            # ``GraphOnlyIndexer`` share, so a rebuilt Project can't wipe an edit.
            desc = _resolve_project_desc(
                ctx.store, ctx.slug, repo_url=repo_url, platform=source
            )

        # 5. Read git snapshot off the IndexingJob (written by clone) and
        #    detect grounder presence on the still-mounted clone dir. Both
        #    are best-effort — historical projects without these signals
        #    render fine; the FE atomic class hides absent values.
        job = ctx.store.get_job(ctx.job_id)
        branch = (job.branch if job else None) or None
        commit_sha = (job.commit_sha if job else None) or None
        commit_short = commit_sha[:7] if commit_sha else None
        maintainer_edited = _detect_grounder(ctx.clone_dir)
        fingerprint = _resolve_index_fingerprint(ctx)
        resolution = _resolve_graph_resolution(ctx)

        # 5b. Completion correctness (GraphRAG ordering law): the
        # knowledge graph is the substrate every downstream feature (Q&A,
        # search, entities) reads. A run that reaches finalize with an EMPTY
        # graph "completed without creating the graph" — that is a FAILURE, not
        # a success. Refuse to finalize and mark the job failed so it surfaces
        # as a real error (distinct from "error after the graph was built",
        # which already lands as failed with a populated graph). Soft-gated:
        # a graph-less install (no backend) is not blocked here.
        with progress.step("finalize.verify_graph"):
            if not _graph_is_populated(ctx):
                err = (
                    "cannot finalize: the knowledge graph is empty or unreadable — "
                    "the graph build did not run, produced no nodes, or the store "
                    "could not be queried to confirm it"
                )
                ctx.store.append_job_event(ctx.job_id, {
                    "type": "error",
                    "error": {"code": "validation", "message": err},
                })
                ctx.store.update_job(ctx.job_id, status="failed", current_file=None)
                return _err_result("validation", err)

        # 6. Build and persist the Project record (upsert).
        from mewbo_graph.wiki.types import Project  # noqa: PLC0415

        indexed_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        prior_project = ctx.store.get_project(ctx.slug)
        project = Project(
            slug=ctx.slug,
            source=source,
            lang=lang,
            indexedAt=indexed_at,
            pages=page_count,
            primary=False,
            desc=desc,
            landingPageId=args.landingPageId,
            repoUrl=repo_url or None,
            host=host,
            branch=branch,
            commitSha=commit_sha,
            commitShort=commit_short,
            maintainerEdited=maintainer_edited,
            fingerprint=fingerprint,
            resolution=resolution,
            stepMeasurements=_measurements_for_finalize(
                prior_project, job.progress if job is not None else None
            ),
        )
        # create_project is upsert in both backends — no duplicate error.
        with progress.step("finalize.persist_project"):
            ctx.store.create_project(project)

            # 7. Update job to complete.
            ctx.store.update_job(
                ctx.job_id,
                status="complete",
                landing_page_id=args.landingPageId,
                current_file=None,
            )

        # 7b. Supersede older non-terminal jobs for this slug. Earlier attempts
        # that halted or were interrupted stay non-terminal (scanning /
        # finalizing / interrupted) forever and keep the project pinned in the
        # "Indexing now" active-jobs list — which HIDES the finished wiki from
        # the gallery (the FE suppresses a completed tile while its slug has an
        # active job). A completed index makes those attempts moot; mark them
        # terminally failed so the completed project surfaces immediately.
        with progress.step("finalize.supersede"):
            _supersede_stale_jobs(ctx)

            # 7c. Supersede prior-commit ARTIFACTS. ``upsert_nodes`` never deletes by
            # slug, so without this reap the store would be the UNION of every commit
            # ever indexed — a file deleted months ago still served to retrieval, and
            # ``node_count`` meaningless as "the graph for this commit". Every
            # node/edge/entity carries its commit, so a completed index reaps every
            # OTHER commit's graph + entity artifacts for the slug (``None``-stamped
            # rows — QA-minted entities — are preserved). Pages are already pruned to
            # this run's plan above, so they are not swept here. Best-effort: a store
            # hiccup here must not undo the index that just succeeded.
            if commit_sha:
                try:
                    reaped = ctx.store.supersede_graph_artifacts(
                        ctx.slug, keep_commit_sha=commit_sha
                    )
                    total = sum(reaped.values())
                    if total:
                        emit_log(
                            ctx,
                            f"Superseded {total} artifact(s) from prior commits "
                            f"({reaped})",
                        )
                except Exception as exc:  # pragma: no cover — best-effort cleanup
                    logging.info(
                        "wiki_finalize: superseding prior-commit artifacts failed ({})", exc
                    )

        # 8. Emit finalize phase + complete event.
        with progress.step("finalize.publish"):
            emit_phase(ctx, "finalize")
            emit_log(ctx, f"Wiki ready: {page_count} pages, landing on {args.landingPageId}")
            ctx.store.append_job_event(ctx.job_id, {
                "type": "complete",
                "landingPageId": args.landingPageId,
                "pageCount": page_count,
            })

        # A completed job must report fraction 1.0. Most declared steps close
        # through their own ``with progress.step(...)`` scope, but a fan-out
        # reported via ``ProgressReporter.report`` (the ``enrich`` mint loop)
        # opens a step with no closing scope at all, and a phase this run
        # genuinely never reached (a resumed job whose plan/pages skip fires
        # BEFORE the tool that would have set a total or entered a step)
        # otherwise stays ``pending`` forever. This is the ONE place every
        # agent-driven run shape passes through, so it is where the whole
        # ledger is settled.
        # Settling closes the ledger; it does NOT close the question. A step
        # left unreported while its own phase completed means one
        # implementation of that phase reported nothing, and `settle` warns on
        # the timeline itself rather than relying on each of its four call
        # sites to remember — the defect that hid an uninstrumented second scan
        # implementation until a finished job was read off a deployment.
        progress.settle()

        # ``publish`` finishes after the original Project upsert, so fold the
        # whole closed ledger once more. A calibration write is best-effort: it
        # improves the following estimate but must not invalidate this index.
        try:
            completed_job = ctx.store.get_job(ctx.job_id)
            if completed_job is not None:
                calibrated = project.model_copy(update={
                    "step_measurements": _measurements_for_finalize(
                        prior_project, completed_job.progress
                    )
                })
                ctx.store.create_project(calibrated)
        except Exception:
            pass

        # Signal the loop to terminate: no post-finalize LLM turn needed. There
        # is no ephemeral clone-token cache to forget — the durable credential
        # store IS the source of truth and re-index needs it to persist.
        self._terminate_run_pending = True

        return MockSpeaker(content=str({
            "complete": True,
            "landingPageId": args.landingPageId,
            "pageCount": page_count,
            "maintainerEdited": maintainer_edited,
            "branch": branch,
            "commitSha": commit_sha,
        }))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _measurements_for_finalize(
    project: Any, ledger: Any, *, now: datetime.datetime | None = None
) -> dict[str, Any]:
    """Return the current project's calibrated measurements after this ledger.

    Cost: ``O(declared steps)``. Only declared ``done`` records update the
    snapshot; skipped resume phases retain their prior reading, because their
    near-zero elapsed time describes reused work rather than its actual cost.
    """
    if ledger is None:
        return getattr(project, "step_measurements", {}) if project is not None else {}
    from mewbo_graph.wiki.types import Project  # noqa: PLC0415

    declared_keys = {spec.key for specs in PHASE_STEPS.values() for spec in specs}
    prior = project
    if prior is None:
        # The method belongs on Project, but a first index has no project row to
        # receive it yet. A minimal in-memory row supplies the same empty prior.
        prior = Project(
            slug="",
            source="git",
            lang="",
            indexedAt="",
            pages=0,
            desc="",
        )
    return prior.measured_steps(
        ledger.steps,
        declared_keys=declared_keys,
        now=now or datetime.datetime.now(datetime.timezone.utc),
    )


def _host_from_url(url: str) -> str | None:
    """Return the DNS host from *url*; ``None`` when unparseable.

    No platform guessing — host is just the host. The wizard tells us
    *which software* (gitea/github/gitlab/…) runs there; we trust that.
    """
    if not url:
        return None
    try:
        return urlparse(url).hostname or None
    except Exception:
        return None


def _split_owner_repo(slug: str) -> tuple[str, str] | None:
    """Pull ``(owner, repo)`` from a slug of any segment depth.

    Delegates to :class:`CredentialScope`, which owns the slug grammar (last two
    segments = owner/repo; trailing ``.git`` stripped; a 2-segment ``owner/repo``
    and a GitLab-subgroup ``host/group/sub/proj`` both parse). ``None`` for a
    host-only or unparseable slug — there is no repo to address.
    """
    scope = CredentialScope.coerce(slug)
    if scope is None or scope.owner is None or scope.repo is None:
        return None
    return scope.owner, scope.repo


def _load_submission(ctx: Any) -> dict[str, Any] | None:
    """Read the persisted wizard submission for *ctx.job_id*.

    Stored by ``WikiIndexingJob.start`` via ``store.save_job_submission``
    using ``by_alias=True`` — keys are camelCase
    (``repoUrl``/``platform``/``filterMode``).
    """
    try:
        return ctx.store.get_job_submission(ctx.job_id)
    except Exception:
        return None


def _resolve_description(store: Any, slug: str, *, repo_url: str, platform: str) -> str:
    """Best-effort repo description, resolved through the durable credential chain.

    The description fetch is a SEPARATE process step from the clone, so it has no
    git "winner" to inherit — it hands :func:`_fetch_description` the store+slug
    and lets the shared chain-aware fetch walk the credentials itself (one retry
    policy, in ``_platform_api.api_get_json_with_chain``). A revoked stored token
    that 401s the API no longer shadows a valid ambient one.
    """
    return _fetch_description(
        repo_url=repo_url, platform=platform, token=None, slug=slug, store=store
    )


def _resolve_project_desc(store: Any, slug: str, *, repo_url: str, platform: str) -> str:
    """The description a (re)index should persist — user override wins.

    THE read-preserve seam, shared by ``wiki_finalize`` AND ``GraphOnlyIndexer``
    so neither carries its own copy of the tiers below. ``Project`` is rebuilt
    wholesale at every finalize, so without this an edited description would be
    silently overwritten by the platform fetch on the very next reindex.

    Precedence:

    1. ``ProjectSettings.desc`` — an explicit user edit. Durable, survives reindex.
    2. The platform API fetch (today's default: the repo's own description).
    3. The previous ``Project`` record's ``desc`` — so a token-less refresh against
       a private host, where the fetch returns "", doesn't blow away a description
       a previous successful run wrote.

    A store with no settings record yields ``None`` at tier 1 and falls through.
    """
    try:
        settings = store.get_project_settings(slug)
    except Exception:  # pragma: no cover — never fail an index on the settings read
        settings = None
    if settings is not None and settings.desc:
        return settings.desc

    desc = _resolve_description(store, slug, repo_url=repo_url, platform=platform)
    if not desc:
        existing = store.get_project(slug)
        if existing is not None and existing.desc:
            desc = existing.desc
    return desc


def _resolve_index_fingerprint(ctx: Any) -> IndexFingerprint | None:
    """The fingerprint THIS run's graph phase stamped on the job, or ``None``.

    A READ-ONLY copy, never a re-probe: ``build_graph_core`` is the only place
    that computes an ``IndexFingerprint`` — it stamps ``IndexingJob.fingerprint``
    at the moment it actually builds the graph, not here. Re-probing live at
    finalize would answer "what is available NOW", not "what built the
    artifacts actually in the store" — the two disagree exactly when a resume
    skipped the ``graph`` phase (``ResumePlan.should_skip("graph")``) because
    an earlier invocation of this SAME job already built it; that earlier
    invocation's stamp survives on the job record because a resume reuses the
    same ``job_id``, so reading it here (rather than re-deriving) is what
    makes the fingerprint describe the graph that is actually persisted.

    Shared between ``wiki_finalize`` AND ``GraphOnlyIndexer`` — the same
    read-preserve seam ``_resolve_project_desc`` already is for ``desc`` — so
    the two ``Project`` write sites can never disagree about where a
    fingerprint comes from.

    ``None`` is a legitimate answer, not a failure: a job whose stamp was lost
    to ``update_job``'s unlocked read-modify-write (a concurrent Cancel, most
    commonly) simply never recorded one. A ``Project`` with no fingerprint reads exactly the
    same as one with a mismatched one downstream — "cannot compare, full
    rebuild" — never a silent match.
    """
    job = ctx.store.get_job(ctx.job_id)
    return job.fingerprint if job is not None else None


def _resolve_graph_resolution(ctx: Any) -> GraphResolution | None:
    """The resolver outcome THIS run's graph phase stamped, or ``None``.

    The same read-preserve seam as :func:`_resolve_index_fingerprint`, for the
    same reason and with the same failure mode. ``build_graph_core`` writes the
    project row directly when it finishes, but ``Project`` is constructed
    WHOLESALE here — so without this read a first index would persist no
    outcome at all (no row existed to update), and every re-index would
    overwrite a good one back to ``None``.

    ``None`` stays a legitimate answer: a resume that skipped the ``graph``
    phase, or a lost ``update_job`` patch. It reads downstream as "unknown",
    never as "resolution was fine".
    """
    job = ctx.store.get_job(ctx.job_id)
    return job.resolution if job is not None else None


def _fetch_description(
    *,
    repo_url: str,
    platform: str,
    token: str | None,
    slug: str,
    store: Any = None,
) -> str:
    """Best-effort fetch of repo description from the platform's public API.

    Returns an empty string on any failure — the description is purely
    cosmetic; never block indexing on this. Shares the auth-header table + GHE
    base branch + private-TLD TLS carve-out + guarded fetch + CHAIN RETRY with
    ``freshness._compare_behind`` via ``_platform_api``.

    *token* is the PREFERRED credential (tried first); *store* lets the fetch fall
    through to the rest of the chain — and finally to an anonymous read — when the
    remote REFUSES that one. With no *store* it is a single-credential fetch.

    Endpoints used per platform:

    - github   : ``GET {api.github.com|<host>/api/v3}/repos/{owner}/{repo}`` → ``description``
    - gitea    : ``GET {origin}/api/v1/repos/{owner}/{repo}`` → ``description``
                  (works for self-hosted Gitea/Forgejo too)
    - gitlab   : ``GET {origin}/api/v4/projects/{owner%2Frepo}`` → ``description``
    - bitbucket: ``GET https://api.bitbucket.org/2.0/repositories/{owner}/{repo}`` → ``description``
    - azure / git: skipped (no portable description endpoint)
    """
    if not repo_url:
        return ""
    owner_repo = _split_owner_repo(slug)
    if owner_repo is None:
        return ""
    owner, repo = owner_repo
    parsed = urlparse(repo_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api_url: str | None = None
    # API endpoint shape is determined by *platform* (the software), not host.
    # github.com and github.enterprise.acme.io both use the GitHub v3/v4 shape.
    if platform == "github":
        api_url = f"{github_api_base(parsed)}/repos/{owner}/{repo}"
    elif platform == "gitea":
        api_url = f"{origin}/api/v1/repos/{owner}/{repo}"
    elif platform == "gitlab":
        api_url = f"{origin}/api/v4/projects/{quote(f'{owner}/{repo}', safe='')}"
    elif platform == "bitbucket":
        # Bitbucket uses Basic with app passwords; token-only is harder to
        # construct portably — public, description-only fetch (no auth header).
        api_url = f"https://api.bitbucket.org/2.0/repositories/{owner}/{repo}"

    if api_url is None:
        return ""
    data = api_get_json_with_chain(
        api_url,
        platform=platform,
        private_host=_is_private_host(repo_url),
        store=store,
        slug=slug,
        preferred_token=token,
    )
    if data is None:
        return ""
    desc = data.get("description") or ""
    return desc.strip() if isinstance(desc, str) else ""


def _detect_grounder(clone_dir: Any) -> bool:
    """Return True iff a known grounder file exists at the top of *clone_dir*.

    Presence-only signal — the file's parsed contents are *not* required.
    Anchored to the same path list ``wiki_load_grounder`` walks, so badge
    state and grounder-adoption never disagree.
    """
    try:
        return any((clone_dir / rel).exists() for rel in _DEFAULT_GROUNDER_PATHS)
    except Exception:
        return False


def _graph_is_populated(ctx: Any) -> bool:
    """True iff THIS job's commit built at least one node for ``ctx.slug``.

    Scoped to ``ctx.commit_sha``, and that is the whole point of the gate. Read
    unscoped it asked "has any commit ever built a graph for this slug", which
    a re-index of a previously-indexed repo answers ``True`` for before doing
    any work — so the gate passed precisely in the case it exists to catch. A
    job with no commit stamped (a commit-less catalog path) has one generation,
    so the union is that generation and ``every()`` is the honest scope.

    The two failure shapes are NOT the same question, and conflating them makes
    this gate toothless:

    - ``NotImplementedError`` — the graph BACKEND is absent, by design, on a
      lean graph-less install. That is a known capability gap rather than
      uncertainty, and a BM25-only wiki must still be allowed to finalize.
    - Anything else — the read did not happen, so nothing here knows whether the
      graph is populated. That fails CLOSED: answering ``True`` would let a
      transient store error launder an unverified (possibly empty) graph into a
      ``complete`` index. Refusing costs one re-drive; the recovery path re-runs
      finalize once the store answers again.
    """
    from mewbo_graph.wiki.types import CommitScope  # noqa: PLC0415

    commit = getattr(ctx, "commit_sha", None)
    try:
        if commit:
            # ``count_graph_nodes`` is the purpose-built commit-scoped counter
            # the resume skip predicate already keys on, so the gate and the
            # skip decision cannot disagree about what "this commit built a
            # graph" means — and it answers with a count instead of
            # materialising every node just to test non-emptiness.
            return ctx.store.count_graph_nodes(ctx.slug, commit_sha=commit) > 0
        return bool(ctx.store.query_graph(ctx.slug, scope=CommitScope.every()))
    except NotImplementedError:
        return True  # graph backend absent by design — don't block finalize
    except Exception as exc:
        logging.warning(
            "wiki_finalize: graph check FAILED for {} ({}: {}) — refusing to finalize",
            ctx.slug, type(exc).__name__, exc,
        )
        return False


def _supersede_stale_jobs(ctx: Any) -> None:
    """Retire this slug's other jobs — status AND their on-disk checkouts.

    A completed index retires earlier stuck attempts so they drop out of the
    active-jobs surface and stop hiding the finished project. Their clone
    directories go with them, and this is the seam for it: the clone root is keyed
    by job_id, so every re-index and every resume that mints a job leaves a full
    working copy behind that nothing else ever revisits — they accumulate into
    gigabytes of long-dead directories. A job terminating is the natural moment to
    reap, and reaping HERE keeps it a bounded, per-slug sweep rather than a daemon
    walking a shared root.

    The just-finished job keeps its checkout: it is now the newest ``complete``
    job for the slug, which is exactly the one ``resolve_qa_clone_dir`` hands to
    the Q&A source-reading tools. Reaping the rest makes that resolution
    unambiguous as a side effect.

    Best-effort throughout: neither a store hiccup nor an undeletable directory
    may undo the index that just succeeded.
    """
    terminal = {"complete", "failed", "cancelled"}
    try:
        siblings = ctx.store.list_jobs(ctx.slug)
    except Exception as exc:  # pragma: no cover — best-effort cleanup
        logging.info("wiki_finalize: list_jobs for supersede failed ({})", exc)
        return
    for job in siblings:
        if job.job_id == ctx.job_id:
            continue
        if job.status not in terminal:
            try:
                ctx.store.update_job(job.job_id, status="failed")
                ctx.store.append_job_event(job.job_id, {
                    "type": "error",
                    "error": {
                        "code": "internal",
                        "message": "superseded by a newer completed index",
                    },
                })
            except Exception as exc:  # pragma: no cover — best-effort
                logging.info("wiki_finalize: supersede {} failed ({})", job.job_id, exc)
        _reap_clone_dir(job.job_id)


def _reap_clone_dir(job_id: str) -> None:
    """Delete the on-disk checkout of a job that is no longer the live one.

    Resolves the path through the same ``_clone_dir_for`` every wiki tool uses, so
    a relocated clone root (``MEWBO_WIKI_CLONE_ROOT``) is honoured and this can
    never delete outside it. Never raises — a busy or unwritable directory is left
    for the next completion to retry.
    """
    try:
        target = _clone_dir_for(job_id)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
    except Exception as exc:  # pragma: no cover — best-effort
        logging.info("wiki_finalize: reaping clone dir for {} failed ({})", job_id, exc)


__all__ = [
    "WikiFinalizeArgs",
    "WikiFinalizeTool",
    "_host_from_url",
    "_split_owner_repo",
    "_fetch_description",
    "_resolve_description",
    "_resolve_project_desc",
    "_resolve_index_fingerprint",
    "_resolve_graph_resolution",
    "_detect_grounder",
    "_graph_is_populated",
    "_reap_clone_dir",
    "_supersede_stale_jobs",
    "_load_submission",
]
