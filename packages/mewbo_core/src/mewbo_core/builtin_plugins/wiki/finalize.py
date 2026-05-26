"""``wiki_finalize`` SessionTool — persist Project record, emit complete event."""
from __future__ import annotations

import datetime
import json
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mewbo_core.builtin_plugins.wiki._ctx import emit_log, emit_phase, resolve_job_ctx
from mewbo_core.builtin_plugins.wiki.clone import _err_result, _is_private_host, _resolve_runtime
from mewbo_core.builtin_plugins.wiki.grounder import _DEFAULT_GROUNDER_PATHS
from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from mewbo_core.session_tools import DEFAULT_SESSION_TOOL_MODES

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep
    from mewbo_core.types import Event

logging = get_logger(name="core.builtin_plugins.wiki.finalize")


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


class WikiFinalizeTool:
    """SessionTool: finalize indexing — persist Project record, emit complete event."""

    tool_id = "wiki_finalize"
    modes = DEFAULT_SESSION_TOOL_MODES
    schema: dict[str, Any] = pydantic_to_openai_tool(WikiFinalizeArgs, name="wiki_finalize")

    def __init__(
        self,
        session_id: str,
        event_logger: Callable[[Event], None] | None = None,
    ) -> None:
        """Initialise the tool with the owning session id and optional event logger."""
        self._session_id = session_id
        self._event_logger = event_logger
        self._terminate = False

    def should_terminate_run(self) -> bool:
        """Return True once if the run should terminate; resets the flag."""
        v, self._terminate = self._terminate, False
        return v

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_finalize`` tool call."""
        # 1. Resolve runtime and job ctx.
        runtime = _resolve_runtime()
        ctx = resolve_job_ctx(self._session_id, runtime) if runtime is not None else None
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        raw = action_step.tool_input if isinstance(action_step.tool_input, dict) else {}
        try:
            args = WikiFinalizeArgs.model_validate(raw)
        except ValidationError as ve:
            return _err_result("validation", str(ve))

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
        try:
            from mewbo_api.wiki.jobs import pop_clone_token  # noqa: PLC0415
            token = pop_clone_token(ctx.job_id) or submission.get("token") or None
        except Exception:
            token = submission.get("token") or None
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

        # 6. Build and persist the Project record (upsert).
        from mewbo_api.wiki.types import Project  # noqa: PLC0415

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

        # 8. Emit finalize phase + complete event.
        emit_phase(ctx, "finalize")
        emit_log(ctx, f"Wiki ready: {page_count} pages, landing on {args.landingPageId}")
        ctx.store.append_job_event(ctx.job_id, {
            "type": "complete",
            "landingPageId": args.landingPageId,
            "pageCount": page_count,
        })

        # 9. Forget the cached clone-time token now that the job is done.
        try:
            from mewbo_api.wiki.jobs import forget_clone_token  # noqa: PLC0415
            forget_clone_token(ctx.job_id)
        except Exception:
            pass

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


__all__ = [
    "WikiFinalizeArgs",
    "WikiFinalizeTool",
    "_host_from_url",
    "_split_owner_repo",
    "_fetch_description",
    "_detect_grounder",
    "_load_submission",
]
