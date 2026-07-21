"""``RepoFreshness`` — compare a wiki's indexed commit against the remote HEAD.

Atomic static class behind ``GET /v1/wiki/projects/<slug>/freshness``. Two steps,
both authenticated through the SAME durable credential chain the clone uses
(``resolve_chain`` → repo scope → host scope → ambient git credential), with the
git-credential helper disabled and prompts off so a private/self-hosted host
never wedges on the read-only mounted credential file:

1. ``git ls-remote`` the remote's HEAD (or ``refs/heads/<ref>``) → ``remote_sha``.
2. If it differs from the indexed sha, ask the platform's compare API HOW FAR
   behind (gitea ``total_commits`` / github ``ahead_by`` / gitlab ``len(commits)``).

Both steps share code with the rest of the wiki git integrations: the ls-remote
runs through :func:`clone.run_git_with_chain` (the ONE credential-iterating
subprocess executor), and the compare fetch reuses ``_platform_api`` (the SAME
auth-header table + GHE branch + TLS carve-out ``finalize._fetch_description``
uses). The compare call is authenticated with the token that ACTUALLY won the
ls-remote (the executor's ``winner``), never a blindly re-resolved first token.

Everything is best-effort: an unreachable remote yields ``up_to_date=None`` (could
not check); a reachable-but-uncomparable remote yields ``behind_by=None`` with
``up_to_date=False`` (we KNOW the sha moved, we just can't count the commits).
Never raises into the route — the FE renders whatever partial signal comes back.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlparse

from mewbo_core.common import get_logger
from pydantic import BaseModel

from mewbo_graph.plugins.wiki._platform_api import (
    api_get_json_with_chain,
    github_api_base,
)
from mewbo_graph.plugins.wiki.clone import (
    _is_private_host,
    build_ls_remote_command,
    run_git_with_chain,
)
from mewbo_graph.plugins.wiki.finalize import _split_owner_repo

logging = get_logger(name="mewbo_graph.plugins.wiki.freshness")


class FreshnessResult(BaseModel):
    """The freshness comparison for one project.

    ``behind_by`` is ``None`` when the platform compare is unavailable/failed (the
    remote moved but we can't count by how much); ``up_to_date`` is ``None`` when
    the check could not run at all (remote unreachable).
    """

    remote_sha: str | None
    behind_by: int | None
    up_to_date: bool | None
    error: str | None


class RepoFreshness:
    """Compare a project's indexed commit against its remote (ls-remote + compare)."""

    @staticmethod
    def check(
        repo_url: str,
        indexed_sha: str,
        *,
        ref: str | None = None,
        platform: str | None = None,
        store: Any = None,
        slug: str | None = None,
    ) -> FreshnessResult:
        """Return the freshness of *indexed_sha* against *repo_url*'s remote HEAD/ref."""
        private = _is_private_host(repo_url)
        target_ref = f"refs/heads/{ref}" if ref else "HEAD"
        outcome = run_git_with_chain(
            store,
            slug or "",
            repo_url,
            lambda authed: build_ls_remote_command(
                authed, target_ref, private_host=private
            ),
            timeout=30,
        )
        if not outcome.ok:
            # Could not reach the remote at all — unknown, not "behind".
            return FreshnessResult(
                remote_sha=None, behind_by=None, up_to_date=None,
                error=outcome.stderr_redacted,
            )
        remote_sha = _first_sha(outcome.stdout)
        if remote_sha is None:
            # Reachable, but the ref does not exist on the remote — nothing to
            # compare against (not an error, but not a known state either).
            return FreshnessResult(
                remote_sha=None, behind_by=None, up_to_date=None, error=None
            )
        if remote_sha == indexed_sha:
            return FreshnessResult(
                remote_sha=remote_sha, behind_by=0, up_to_date=True, error=None
            )
        # The remote moved. Count how far behind via the platform API. The
        # ls-remote winner is only a HINT (a public repo's git endpoint answers
        # a revoked token too) — the compare fetch re-walks the chain itself.
        behind = _compare_behind(
            repo_url=repo_url,
            platform=platform or "",
            base=indexed_sha,
            head=remote_sha,
            token=outcome.winner.token if outcome.winner else None,
            slug=slug or "",
            store=store,
        )
        if behind is None:
            # We KNOW it's not up to date (sha changed) — just can't count commits.
            return FreshnessResult(
                remote_sha=remote_sha, behind_by=None, up_to_date=False, error=None
            )
        return FreshnessResult(
            remote_sha=remote_sha,
            behind_by=behind,
            up_to_date=behind == 0,
            error=None,
        )


# ---------------------------------------------------------------------------
# ls-remote result parsing
# ---------------------------------------------------------------------------


def _first_sha(stdout: str) -> str | None:
    """Return the first sha from ``git ls-remote`` stdout, or ``None`` when empty."""
    out = stdout.strip()
    if not out:
        return None
    return out.splitlines()[0].split("\t", 1)[0].strip() or None


# ---------------------------------------------------------------------------
# Platform compare API (how many commits behind)
# ---------------------------------------------------------------------------


def _compare_behind(
    *,
    repo_url: str,
    platform: str,
    base: str,
    head: str,
    token: str | None,
    slug: str,
    store: Any = None,
) -> int | None:
    """Return how many commits *head* is ahead of *base*, or ``None`` on failure.

    Uses the platform's compare endpoint (reuses ``_platform_api`` — same auth
    header table + GHE base branch + private-TLD TLS carve-out + guarded fetch as
    ``finalize._fetch_description``):

    - gitea  : ``GET {origin}/api/v1/repos/{o}/{r}/compare/{base}...{head}`` → ``total_commits``
    - github : ``GET {api}/repos/{o}/{r}/compare/{base}...{head}`` → ``ahead_by``
    - gitlab : ``GET {origin}/api/v4/projects/{o%2Fr}/repository/compare`` → ``len(commits)``

    Any other platform (bitbucket/azure/git) has no portable compare shape →
    ``None`` (the FE still shows "update available" from the differing sha).
    """
    owner_repo = _split_owner_repo(slug)
    if not repo_url or owner_repo is None:
        return None
    owner, repo = owner_repo
    parsed = urlparse(repo_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    api_url: str | None = None
    key: str | None = None

    if platform == "gitea":
        api_url = f"{origin}/api/v1/repos/{owner}/{repo}/compare/{base}...{head}"
        key = "total_commits"
    elif platform == "github":
        api_url = f"{github_api_base(parsed)}/repos/{owner}/{repo}/compare/{base}...{head}"
        key = "ahead_by"
    elif platform == "gitlab":
        proj = quote(f"{owner}/{repo}", safe="")
        api_url = (
            f"{origin}/api/v4/projects/{proj}/repository/compare"
            f"?from={quote(base, safe='')}&to={quote(head, safe='')}"
        )
        key = "commits"  # a list → len()

    if api_url is None or key is None:
        return None
    # *token* is the credential that authenticated the ls-remote — but a public
    # repo's git endpoint answers even a REVOKED token, so the git winner is only
    # a HINT here. The chain-aware fetch tries it first, then falls through to the
    # remaining chain credentials (and finally anonymous) when the remote REFUSES
    # it — see _platform_api.api_get_json_with_chain.
    data = api_get_json_with_chain(
        api_url,
        platform=platform,
        private_host=_is_private_host(repo_url),
        store=store,
        slug=slug,
        preferred_token=token,
    )
    if data is None:
        logging.info("freshness: compare fetch unavailable for {}", slug)
        return None

    value = data.get(key)
    if key == "commits":
        return len(value) if isinstance(value, list) else None
    return int(value) if isinstance(value, (int, float)) else None


__all__ = ["FreshnessResult", "RepoFreshness"]
