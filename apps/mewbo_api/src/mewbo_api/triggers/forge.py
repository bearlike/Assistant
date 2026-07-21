"""Forge REST polling for ci.workflow / forge.pr triggers (WP3).

A ``TriggerService`` fires *event-driven* trigger kinds (``ci.workflow``,
``forge.pr``) by polling the originating forge's REST API and normalizing each
response into the exact payload dict the matching :class:`TriggerSpec` subclass
documents on its ``matches()`` method — the service never re-implements the
match logic, it only fetches state and hands it to ``spec.matches(payload)``.

Credentials reuse **the one forge-token seam the product already has**:
``channels.vcs.tokens`` keyed by forge host (the same store ``vcs_pickup`` posts
reply comments through). We never hand-roll a second credential path. The forge
host for a bare ``owner/name`` trigger repo is resolved through
:class:`~mewbo_api.repo_identity.RepoIdentity` over the configured projects
(authoritative), falling back to the sole configured token host when exactly one
is set.

Honest scope note: polling cleanly detects **terminal, idempotent** forge state
— a completed CI run (status/conclusion) and a merged PR / settled combined CI
status. It deliberately does NOT synthesize ``review`` / ``comment`` PR events:
without a per-item watermark, re-emitting "a comment exists" every poll is either
spammy or a lie, and those events are already delivered by the CI-side
``vcs-pickup`` mention path. A ``forge.pr`` trigger listing only ``review`` /
``comment`` simply never fires from polling — use a ``webhook`` trigger instead.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Protocol

from mewbo_core.common import get_logger

from mewbo_api.repo_identity import RepoIdentity

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

    from mewbo_core.config import AppConfig

logging = get_logger(name="api.triggers.forge")

# Forge-native terminal CI statuses (Gitea Actions reports a single terminal
# word in the run's ``status`` field, whereas GitHub separates
# ``status="completed"`` + ``conclusion``). When a run carries no explicit
# ``conclusion`` but its status is one of these, we treat the status AS the
# conclusion and normalize ``status -> "completed"`` so a Gitea run satisfies the
# GitHub-shaped payload ``CiWorkflowTrigger.matches`` expects.
_TERMINAL_CI_STATUSES: frozenset[str] = frozenset(
    {"success", "failure", "cancelled", "skipped", "error", "timed_out", "neutral", "stale"}
)

# Terminal combined-CI-status states that mean "CI settled" for a forge.pr
# ``ci_status`` event.
_TERMINAL_PR_CI_STATES: frozenset[str] = frozenset({"success", "failure", "error"})

_GET_TIMEOUT_S = 15


class ForgeClient(Protocol):
    """The forge-state read surface a ``TriggerService`` polls.

    Both methods return a list of *already-normalized* payload dicts; the
    service filters them through ``spec.matches(payload)``. An implementation
    raises on a transport/HTTP error so the service can bound consecutive
    failures — it never returns a partial/ambiguous result silently.
    """

    def poll_ci_runs(self, repo: str, *, ref: str | None = None) -> list[dict[str, Any]]:
        """Return normalized CI-run payloads for *repo*.

        Each payload has the shape ``CiWorkflowTrigger.matches`` documents:
        ``{repo, run_id, workflow, ref, status, conclusion}``.
        """
        ...

    def poll_pr(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Return normalized PR-event payloads for ``repo#number``.

        Each payload has the shape ``ForgePrTrigger.matches`` documents:
        ``{repo, number, event}`` with ``event`` in ``{merged, ci_status}``
        (see the module docstring on why review/comment are not polled).
        """
        ...


class HttpForgeClient:
    """HTTP :class:`ForgeClient` over one forge's REST API (GitHub / Gitea).

    ``GET /repos/{owner}/{repo}/...`` + ``Authorization: token`` are identical
    on GitHub and Gitea, so one client covers both — exactly like
    ``VcsPickupService.post_comment``'s single reply client.
    """

    def __init__(
        self,
        *,
        api_url: str,
        token: str,
        tls_verify: bool = True,
    ) -> None:
        """Bind the resolved forge base URL + bot token + TLS posture."""
        self._api_url = api_url.rstrip("/")
        self._token = token
        self._tls_verify = tls_verify

    # -- transport ---------------------------------------------------------

    def _get(self, path: str) -> Any:
        """GET ``{api_url}{path}`` and return the decoded JSON body.

        Raises on transport / non-2xx (the service treats it as a poll error);
        a 404 returns ``None`` (the resource is simply absent, not a failure).
        """
        url = f"{self._api_url}{path}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"token {self._token}",
            },
        )
        ssl_ctx = ssl.create_default_context()
        if not self._tls_verify:  # opt-out for untrusted internal CAs
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=_GET_TIMEOUT_S, context=ssl_ctx) as resp:  # noqa: S310
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    # -- CI runs -----------------------------------------------------------

    def poll_ci_runs(self, repo: str, *, ref: str | None = None) -> list[dict[str, Any]]:
        """Fetch + normalize recent Actions runs for *repo*."""
        body = self._get(f"/repos/{repo}/actions/runs?limit=30")
        runs = self._extract_run_list(body)
        return [self._normalize_ci_run(run, repo) for run in runs]

    @staticmethod
    def _extract_run_list(body: Any) -> list[dict[str, Any]]:
        """Pull the run array out of a forge Actions response, shape-tolerantly."""
        if isinstance(body, dict):
            for key in ("workflow_runs", "runs", "tasks"):
                value = body.get(key)
                if isinstance(value, list):
                    return [r for r in value if isinstance(r, dict)]
            return []
        if isinstance(body, list):
            return [r for r in body if isinstance(r, dict)]
        return []

    @staticmethod
    def _normalize_ci_run(run: dict[str, Any], repo: str) -> dict[str, Any]:
        """Map a forge-native run dict onto the ``CiWorkflowTrigger`` payload shape."""
        raw_status = run.get("status")
        conclusion = run.get("conclusion")
        # Gitea-style single-word terminal status carries no separate
        # conclusion — promote it so the GitHub-shaped matcher sees
        # status="completed" + conclusion=<word>.
        is_terminal_word = isinstance(raw_status, str) and raw_status in _TERMINAL_CI_STATUSES
        if conclusion is None and is_terminal_word:
            conclusion = raw_status
            status: Any = "completed"
        else:
            status = raw_status
        run_id = run.get("id")
        normalized_run_id: Any = run_id
        if isinstance(run_id, (int, str)) and str(run_id).isdigit():
            normalized_run_id = int(run_id)
        return {
            "repo": repo,
            "run_id": normalized_run_id,
            "workflow": run.get("name") or run.get("workflow_name"),
            "ref": run.get("head_branch") or run.get("ref"),
            "status": status,
            "conclusion": conclusion,
        }

    # -- PR events ---------------------------------------------------------

    def poll_pr(self, repo: str, number: int) -> list[dict[str, Any]]:
        """Fetch a PR + its combined CI status; emit merged / ci_status events."""
        pr = self._get(f"/repos/{repo}/pulls/{number}")
        if not isinstance(pr, dict):
            return []
        events: list[dict[str, Any]] = []
        if pr.get("merged") is True:
            events.append({"repo": repo, "number": number, "event": "merged"})
        head_sha = ((pr.get("head") or {}) if isinstance(pr.get("head"), dict) else {}).get("sha")
        if head_sha:
            state = self._combined_ci_state(repo, str(head_sha))
            if state in _TERMINAL_PR_CI_STATES:
                events.append({"repo": repo, "number": number, "event": "ci_status"})
        return events

    def _combined_ci_state(self, repo: str, sha: str) -> str | None:
        """Return the combined commit-status state for *sha* (``success``/…), or None."""
        body = self._get(f"/repos/{repo}/commits/{sha}/status")
        if isinstance(body, dict):
            state = body.get("state")
            return str(state) if state is not None else None
        return None


# ---------------------------------------------------------------------------
# Repo -> forge resolution + factory
# ---------------------------------------------------------------------------


class ForgeClientFactory:
    """Resolves a bare ``owner/name`` trigger repo to an :class:`HttpForgeClient`.

    Atomic factory: the ``AppConfig`` is its state, the repo→host→client
    resolution its methods. It is ITSELF the ``repo -> ForgeClient | None``
    callable the :class:`TriggerService` polls with (``__call__``), so the
    service stays forge-agnostic and tests inject a fake callable instead.
    Resolution is per-call — polling is coarse enough (``poll_interval_seconds``)
    that caching clients isn't worth the staleness.
    """

    def __init__(self, config: AppConfig) -> None:
        """Close over the app config the resolution reads."""
        self._config = config

    @staticmethod
    def _api_base_for_host(host: str) -> str:
        """Build the REST base URL for a forge host (GitHub vs self-hosted Gitea).

        Mirrors ``VcsPickupService.handle``'s github-vs-gitea host heuristic.
        """
        if host == "github.com" or host.endswith(".github.com"):
            return "https://api.github.com"
        return f"https://{host}/api/v1"

    def _host_for_repo(self, repo: str) -> str | None:
        """Resolve the forge host for a bare ``owner/name`` trigger repo.

        Scans configured projects' git remotes (the authoritative source),
        matching *repo* against each project's :class:`RepoIdentity` alias forms
        and returning the identity's host. Mirrors
        ``VcsPickupService._config_project_for_repo``'s identity scan — the ONE
        repo→identity resolver the app already trusts.
        """
        for cfg in self._config.projects.values():
            path = getattr(cfg, "path", None)
            if not path:
                continue
            try:
                for identity in RepoIdentity.for_path(path):
                    if identity.host and repo in identity.aliases():
                        return identity.host
            except Exception:  # pragma: no cover - unreadable project dir
                continue
        return None

    def resolve(self, repo: str) -> HttpForgeClient | None:
        """Build an :class:`HttpForgeClient` for *repo*, or ``None`` if unresolvable.

        Resolution: forge host via configured projects → sole configured token
        host fallback → give up. Token + TLS posture come from
        ``channels.vcs.tokens`` / ``channels.vcs.tls_verify`` (the shared vcs
        credential surface).
        """
        vcs_cfg = self._config.channels.get("vcs", {})
        tokens = vcs_cfg.get("tokens") or {}
        if not tokens:
            return None
        host = self._host_for_repo(repo)
        if host is None and len(tokens) == 1:
            host = next(iter(tokens))
        if host is None:
            return None
        token = tokens.get(host)
        if not token:
            return None
        tls_verify = vcs_cfg.get("tls_verify", True) is not False
        return HttpForgeClient(
            api_url=self._api_base_for_host(host),
            token=str(token),
            tls_verify=tls_verify,
        )

    def __call__(self, repo: str) -> ForgeClient | None:
        """The guarded ``repo -> ForgeClient | None`` callable the service polls."""
        try:
            return self.resolve(repo)
        except Exception:  # pragma: no cover - defensive; a resolver bug must not crash the tick
            logging.warning("Forge client resolution failed for {}", repo, exc_info=True)
            return None


def resolve_forge_client(repo: str, config: AppConfig) -> HttpForgeClient | None:
    """Resolve *repo* to an :class:`HttpForgeClient` (thin wrapper over the factory)."""
    return ForgeClientFactory(config).resolve(repo)


def build_forge_client_factory(config: AppConfig) -> Callable[[str], ForgeClient | None]:
    """Return the ``repo -> ForgeClient | None`` callable the service polls with."""
    return ForgeClientFactory(config)


__all__ = [
    "ForgeClient",
    "ForgeClientFactory",
    "HttpForgeClient",
    "build_forge_client_factory",
    "resolve_forge_client",
]
