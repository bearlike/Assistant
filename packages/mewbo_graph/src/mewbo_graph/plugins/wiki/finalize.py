"""``wiki_finalize`` SessionTool — persist Project record, emit complete event."""
from __future__ import annotations

import datetime
import json
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase
from mewbo_graph.plugins.wiki.clone import (  # noqa: F401 — _resolve_runtime is the per-module test seam
    _is_private_host,
    _resolve_runtime,
)
from mewbo_graph.plugins.wiki.grounder import _DEFAULT_GROUNDER_PATHS

if TYPE_CHECKING:
    from mewbo_core.classes import ActionStep

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

        # 3. Verify landingPageId exists in the persisted pages, then drop
        # stale pages from prior runs that aren't in this run's plan. Without
        # this, re-indexing accumulates slug-drifted duplicates ("Auth and
        # Pairing" + "Authentication and Session Security" + ...) because
        # each LLM run picks slightly different page ids for the same topics.
        # The committed plan (from wiki_commit_plan) is the source of truth
        # for what should remain after this run.
        plan = ctx.store.get_job_plan(ctx.job_id) or []
        plan_ids: set[str] = {entry.get("id", "") for entry in plan if entry.get("id")}
        if plan_ids:
            keep = plan_ids | {args.landingPageId}
            dropped = ctx.store.prune_pages(ctx.slug, keep)
            if dropped:
                emit_log(ctx, f"Dropped {dropped} stale page(s) not in this run's plan")
        pages = ctx.store.list_pages(ctx.slug)
        page_ids = {p.id for p in pages}
        if args.landingPageId not in page_ids:
            return _err_result(
                "validation",
                f"landingPageId '{args.landingPageId}' not found in submitted pages "
                f"({sorted(page_ids) or 'none'})",
            )

        page_count = len(pages)

        # 4. Resolve identity from the persisted submission. The wizard
        # is the canonical source: it carries the explicit platform, the
        # full repo URL (host + path), and the language. We do NOT do
        # any URL-host → platform guessing here — that breaks for any
        # enterprise/self-hosted instance the heuristic doesn't know.
        submission = _load_submission(ctx)
        if not submission:
            return _err_result(
                "internal",
                "wiki submission is missing — cannot finalize without canonical identity",
            )
        repo_url = submission.get("repoUrl") or ""
        # The token is stripped from the persisted submission for safety;
        # the original lives in the in-process clone-token cache. Read it
        # here so the description fetch can authenticate to private/internal
        # platforms (e.g. Gitea on internal hosts that reject anon API
        # calls). Cleared once finalize completes.
        from mewbo_graph.wiki.tokens import CloneTokenCache  # noqa: PLC0415

        token = CloneTokenCache.peek(ctx.job_id) or submission.get("token") or None
        source = submission.get("platform") or ""
        lang = submission.get("language") or "en"
        if not source:
            return _err_result(
                "validation",
                "submission.platform is required",
            )
        host = _host_from_url(repo_url)

        # Best-effort: fetch the repository description from the platform's
        # public API so the landing tile carries real context instead of "".
        # On a token-less refresh against a private host the fetch returns
        # ""; in that case keep whatever description the previous successful
        # run wrote rather than blowing it away.
        desc = _fetch_description(repo_url=repo_url, platform=source, token=token, slug=ctx.slug)
        if not desc:
            existing = ctx.store.get_project(ctx.slug)
            if existing is not None and existing.desc:
                desc = existing.desc

        # 5. Read git snapshot off the IndexingJob (written by clone) and
        #    detect grounder presence on the still-mounted clone dir. Both
        #    are best-effort — historical projects without these signals
        #    render fine; the FE atomic class hides absent values.
        job = ctx.store.get_job(ctx.job_id)
        branch = (job.branch if job else None) or None
        commit_sha = (job.commit_sha if job else None) or None
        commit_short = commit_sha[:7] if commit_sha else None
        maintainer_edited = _detect_grounder(ctx.clone_dir)

        # 5b. Completion correctness (GraphRAG ordering law, Gitea #35): the
        # knowledge graph is the substrate every downstream feature (Q&A,
        # search, entities) reads. A run that reaches finalize with an EMPTY
        # graph "completed without creating the graph" — that is a FAILURE, not
        # a success. Refuse to finalize and mark the job failed so it surfaces
        # as a real error (distinct from "error after the graph was built",
        # which already lands as failed with a populated graph). Soft-gated:
        # a graph-less install (no backend) is not blocked here.
        if not _graph_is_populated(ctx):
            err = (
                "cannot finalize: knowledge graph is empty — the graph build "
                "did not run or produced no nodes"
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
        )
        # create_project is upsert in both backends — no duplicate error.
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
        _supersede_stale_jobs(ctx)

        # 8. Emit finalize phase + complete event.
        emit_phase(ctx, "finalize")
        emit_log(ctx, f"Wiki ready: {page_count} pages, landing on {args.landingPageId}")
        ctx.store.append_job_event(ctx.job_id, {
            "type": "complete",
            "landingPageId": args.landingPageId,
            "pageCount": page_count,
        })

        # 9. Forget the cached clone-time token now that the job is done.
        from mewbo_graph.wiki.tokens import CloneTokenCache  # noqa: PLC0415

        CloneTokenCache.forget(ctx.job_id)

        # Signal the loop to terminate: no post-finalize LLM turn needed.
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
    """Pull ``(owner, repo)`` from a fully-qualified or legacy slug.

    Canonical slug is ``host/owner/repo`` — the last two segments are the
    owner and repo. Legacy ``owner/repo`` slugs still parse cleanly.
    """
    parts = [p for p in slug.split("/") if p]
    if len(parts) < 2:
        return None
    return parts[-2], parts[-1].removesuffix(".git")


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


def _fetch_description(*, repo_url: str, platform: str, token: str | None, slug: str) -> str:
    """Best-effort fetch of repo description from the platform's public API.

    Returns an empty string on any failure — the description is purely
    cosmetic; never block indexing on this.

    Endpoints used per platform:

    - github   : ``GET https://api.github.com/repos/{owner}/{repo}`` → ``description``
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
    headers: dict[str, str] = {"Accept": "application/json", "User-Agent": "MewboWiki/1.0"}
    parsed = urlparse(repo_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api_url: str | None = None
    # API endpoint shape is determined by *platform* (the software), not host.
    # github.com and github.enterprise.acme.io both use the GitHub v3/v4 shape.
    if platform == "github":
        # GitHub.com uses api.github.com; GitHub Enterprise lives at <host>/api/v3.
        if parsed.hostname == "github.com":
            api_url = f"https://api.github.com/repos/{owner}/{repo}"
        else:
            api_url = f"{origin}/api/v3/repos/{owner}/{repo}"
        if token:
            headers["Authorization"] = f"Bearer {token}"
    elif platform == "gitea":
        api_url = f"{origin}/api/v1/repos/{owner}/{repo}"
        if token:
            headers["Authorization"] = f"token {token}"
    elif platform == "gitlab":
        api_url = f"{origin}/api/v4/projects/{quote(f'{owner}/{repo}', safe='')}"
        if token:
            headers["PRIVATE-TOKEN"] = token
    elif platform == "bitbucket":
        api_url = f"https://api.bitbucket.org/2.0/repositories/{owner}/{repo}"
        # Bitbucket uses Basic with app passwords; token-only is harder to
        # construct portably — keep public fetch, skip auth header.

    if api_url is None:
        return ""
    try:
        # Self-signed certs on private TLDs (e.g. git.example.home) — same
        # carve-out the clone tool uses for ``http.sslVerify=false``.
        ctx_ssl: ssl.SSLContext | None = None
        if _is_private_host(repo_url):
            ctx_ssl = ssl.create_default_context()
            ctx_ssl.check_hostname = False
            ctx_ssl.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8, context=ctx_ssl) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        json.JSONDecodeError,
        TimeoutError,
        OSError,
    ) as exc:
        logging.info("wiki_finalize: description fetch failed for %s: %s", slug, exc)
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
    """True iff the code graph holds at least one node for ``ctx.slug``.

    The completion-correctness gate: "completed without creating the graph" is
    a failure. Soft signal — if the graph backend is absent (a graph-less
    install raises ``NotImplementedError``) or the query errors transiently, do
    NOT block finalize (the wiki can still serve BM25 pages). Only a
    present-but-EMPTY graph fails the run.
    """
    try:
        return bool(ctx.store.query_graph(ctx.slug))
    except NotImplementedError:
        return True  # graph backend absent by design — don't block finalize
    except Exception as exc:  # pragma: no cover — never fail finalize on a glitch
        logging.info("wiki_finalize: graph check failed for %s (%s); allowing", ctx.slug, exc)
        return True


def _supersede_stale_jobs(ctx: Any) -> None:
    """Mark sibling non-terminal jobs for ``ctx.slug`` as failed (superseded).

    A completed index retires earlier stuck attempts so they drop out of the
    active-jobs surface and stop hiding the finished project. Best-effort: a
    store hiccup must never undo the just-finished index.
    """
    terminal = {"complete", "failed", "cancelled"}
    try:
        siblings = ctx.store.list_jobs(ctx.slug)
    except Exception as exc:  # pragma: no cover — best-effort cleanup
        logging.info("wiki_finalize: list_jobs for supersede failed (%s)", exc)
        return
    for job in siblings:
        if job.job_id == ctx.job_id or job.status in terminal:
            continue
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
            logging.info("wiki_finalize: supersede %s failed (%s)", job.job_id, exc)


__all__ = [
    "WikiFinalizeArgs",
    "WikiFinalizeTool",
    "_host_from_url",
    "_split_owner_repo",
    "_fetch_description",
    "_detect_grounder",
    "_graph_is_populated",
    "_supersede_stale_jobs",
    "_load_submission",
]
