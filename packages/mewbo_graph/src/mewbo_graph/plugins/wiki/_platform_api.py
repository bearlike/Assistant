"""Shared per-platform REST mechanics for the wiki git integrations.

``finalize._fetch_description`` (repo metadata) and ``freshness._compare_behind``
(commit-count) hit the SAME six-platform API surface with the SAME pieces: an
auth-header table (github ``Bearer`` / gitea ``token`` / gitlab ``PRIVATE-TOKEN``;
bitbucket stays description-only — its app-password Basic auth isn't portable
from a bare token), the GitHub Enterprise base-URL branch (``github.com`` →
``api.github.com``; else ``<origin>/api/v3``), the private-TLD self-signed TLS
carve-out, a guarded ``urlopen`` that never raises, and — crucially — the
credential-chain retry in :func:`api_get_json_with_chain`. That shared mechanics
lives here ONCE; each caller supplies only its endpoint path + response key.
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from mewbo_graph.wiki.credentials import resolve_chain

if TYPE_CHECKING:
    from collections.abc import Iterator
    from urllib.parse import ParseResult

#: HTTP statuses that mean "this CREDENTIAL was refused" — the only ones worth
#: retrying with a different one. Any other outcome (network down, 5xx, malformed
#: JSON) fails identically no matter which token we present, so retrying would
#: just burn HTTP calls. ``404`` is deliberate: GitHub/Gitea mask a repo the
#: caller cannot see as "not found", so a scoped-out token reads as a miss and
#: another chain credential may well see it — the REST mirror of the git-side
#: ``is_auth_failure`` "repository not found" marker.
_AUTH_RETRY_STATUSES = frozenset({401, 403, 404})

#: How many CREDENTIALS one authenticated REST call will try before falling
#: through to the anonymous terminal attempt (which is never counted against it).
_MAX_CRED_ATTEMPTS = 3


def platform_headers(platform: str, token: str | None) -> dict[str, str]:
    """Return the request headers for *platform*, including auth when *token* set.

    Bitbucket is intentionally omitted from the auth branch (its app-password
    Basic scheme can't be built from a bare token portably) — it fetches
    anonymously, description-only.
    """
    headers = {"Accept": "application/json", "User-Agent": "MewboWiki/1.0"}
    if not token:
        return headers
    if platform == "github":
        headers["Authorization"] = f"Bearer {token}"
    elif platform == "gitea":
        headers["Authorization"] = f"token {token}"
    elif platform == "gitlab":
        headers["PRIVATE-TOKEN"] = token
    return headers


def github_api_base(parsed: ParseResult) -> str:
    """Return the GitHub API base: ``api.github.com`` for github.com else GHE v3."""
    if parsed.hostname == "github.com":
        return "https://api.github.com"
    return f"{parsed.scheme}://{parsed.netloc}/api/v3"


def _api_get(
    api_url: str, *, headers: dict[str, str], private_host: bool
) -> tuple[dict[str, Any] | None, int | None]:
    """GET *api_url* → ``(data, http_status)``. Never raises.

    ``status`` is the HTTP code when the server ANSWERED with an error, and
    ``None`` for a transport/parse failure. Keeping the code is what lets
    :func:`api_get_json_with_chain` tell "this credential was refused" (worth
    another credential) from "this endpoint is unreachable/broken" (another
    credential changes nothing) — collapsing both to ``None`` is exactly what
    made the old swallow-everything helper unable to retry correctly.

    *private_host* disables TLS verification for self-signed self-hosted certs —
    the same ``.home``/``.local``/… carve-out the git clone uses.
    """
    ctx_ssl: ssl.SSLContext | None = None
    if private_host:
        ctx_ssl = ssl.create_default_context()
        ctx_ssl.check_hostname = False
        ctx_ssl.verify_mode = ssl.CERT_NONE
    try:
        req = urllib.request.Request(api_url, headers=headers)
        with urllib.request.urlopen(req, timeout=8, context=ctx_ssl) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # MUST precede URLError/OSError — HTTPError subclasses both.
        return None, exc.code
    except (
        urllib.error.URLError,
        json.JSONDecodeError,
        TimeoutError,
        OSError,
    ):
        return None, None
    return (data if isinstance(data, dict) else None), None


def _token_candidates(
    store: Any, slug: str, preferred_token: str | None
) -> Iterator[str]:
    """LAZILY yield the DISTINCT token values an authenticated REST call should try.

    *preferred_token* (the credential that won the git operation) first, then each
    token-kind candidate the durable chain yields. Lazy on purpose: advancing to
    the ambient tier forks a 10s ``git credential fill``, and the preferred token
    is usually right — so a call that succeeds on it must never pay for that fork
    (the eager form regressed exactly this).
    """
    seen: set[str] = set()
    if preferred_token:
        seen.add(preferred_token)
        yield preferred_token
    for candidate in resolve_chain(store, slug):
        token = candidate.token
        if token is not None and token not in seen:
            seen.add(token)
            yield token


def api_get_json_with_chain(
    api_url: str,
    *,
    platform: str,
    private_host: bool,
    store: Any,
    slug: str,
    preferred_token: str | None = None,
) -> dict[str, Any] | None:
    """GET *api_url*, re-walking the credential chain when a credential is REFUSED.

    **Winning a git operation does NOT prove a credential is valid.** A PUBLIC
    repo serves ``git clone``/``ls-remote`` even when the injected token is
    revoked — the git layer never challenges it — so ``run_git_with_chain``
    happily returns a DEAD credential as its ``winner``. Handing that winner to an
    AUTHENTICATED REST call (repo description, commits-behind compare) then 401s,
    and the caller silently degrades (no description; ``behind_by=None``) even
    though a later chain credential would have worked. Verified live: a revoked
    repo-scoped token cloned a public repo fine, then 401'd the Gitea compare API
    that the ambient credential answered with ``total_commits=155``.

    So every authenticated REST call re-walks the chain here — the ONE retry
    policy, shared by the description fetch and the freshness compare:

    1. *preferred_token* (the git winner) — usually right, and free when it is.
    2. Each remaining token-kind chain candidate, on an auth REFUSAL only
       (:data:`_AUTH_RETRY_STATUSES`); capped at :data:`_MAX_CRED_ATTEMPTS`.
    3. Anonymous — always the terminal candidate (mirroring ``resolve_chain``'s
       git-side grammar), and never counted against the cap: when every stored
       token has been revoked, a PUBLIC repo still answers unauthenticated.

    A NON-auth failure (network, 5xx, bad JSON) returns immediately — another
    credential cannot fix it. Returns the parsed dict, or ``None`` if every
    candidate failed. At most ``_MAX_CRED_ATTEMPTS + 1`` HTTP calls.
    """
    attempts = 0
    for token in _token_candidates(store, slug, preferred_token):
        data, status = _api_get(
            api_url,
            headers=platform_headers(platform, token),
            private_host=private_host,
        )
        if data is not None:
            return data
        if status not in _AUTH_RETRY_STATUSES:
            return None  # not a credential problem — another token changes nothing
        attempts += 1
        if attempts >= _MAX_CRED_ATTEMPTS:
            break

    # Every credential was refused (or there were none) — try anonymous last.
    data, _status = _api_get(
        api_url,
        headers=platform_headers(platform, None),
        private_host=private_host,
    )
    return data


__all__ = [
    "api_get_json_with_chain",
    "github_api_base",
    "platform_headers",
]
