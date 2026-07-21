"""``wiki_clone_repo`` SessionTool — deterministic git clone + queued event emission."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase, resolve_runtime
from mewbo_graph.wiki.credentials import (
    CredentialCandidate,
    CredentialScope,
    CredentialSource,
    is_auth_failure,
    resolve_chain,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mewbo_core.classes import ActionStep

logging = get_logger(name="mewbo_graph.plugins.wiki.clone")


# ---------------------------------------------------------------------------
# Runtime resolver — module-level so tests can patch it
# ---------------------------------------------------------------------------


def _resolve_runtime() -> Any:
    """Resolve the wiki runtime (the down-only store seam). Patched in tests."""
    return resolve_runtime()


# ---------------------------------------------------------------------------
# Pydantic args schema
# ---------------------------------------------------------------------------


class WikiCloneArgs(BaseModel):
    """Arguments for ``wiki_clone_repo``."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Repo URL (e.g. https://github.com/org/repo).")
    ref: str | None = Field(
        default=None,
        description=(
            "Optional branch or tag to checkout on a FIRST clone; null = the "
            "repo's default branch. IGNORED once the job has a recorded commit "
            "(a resume), which is pinned server-side and cannot be overridden "
            "from here."
        ),
    )
    token: str | None = Field(
        default=None,
        description=(
            "Optional auth token (never persisted). Tried FIRST; on an auth "
            "rejection the chain FALLS THROUGH to the stored repo/host "
            "credential, then the ambient git credential, then anonymous — so a "
            "wrong/expired token here does not by itself fail the clone."
        ),
    )


# ---------------------------------------------------------------------------
# SessionTool implementation
# ---------------------------------------------------------------------------


class WikiCloneRepoTool(WikiSessionTool):
    """SessionTool: shallow-clone the configured repo and emit the ``queued`` event."""

    tool_id = "wiki_clone_repo"
    args_cls = WikiCloneArgs
    schema: dict[str, object] = pydantic_to_openai_tool(WikiCloneArgs, name="wiki_clone_repo")

    async def handle(self, action_step: ActionStep) -> MockSpeaker:
        """Execute a ``wiki_clone_repo`` tool call."""
        # 1. Resolve runtime and job ctx.
        ctx = self._job_ctx()
        if ctx is None:
            return _err_result("internal", "wiki job ctx not found for this session")

        # 2. Parse and validate args.
        args = self._parse_args(WikiCloneArgs, action_step)
        if isinstance(args, MockSpeaker):
            return args

        # 3. Resolve the ref SERVER-SIDE. A job that already recorded a
        # ``commit_sha`` — every resume — is PINNED to it: the graph, entities and
        # pages a resume reuses describe that exact tree, so checking out anything
        # else silently invalidates every phase the resume plan skipped. The pin is
        # sitting right here in ctx, so it is read from there rather than trusted
        # from ``args.ref``: a correctness invariant this load-bearing must not
        # depend on the model passing the right value. ``args.ref`` still selects
        # the branch/tag on a FIRST clone, where nothing is pinned yet.
        job = ctx.store.get_job(ctx.job_id)
        pinned_sha = (job.commit_sha if job is not None else None) or None
        clone_dir = ctx.clone_dir

        emit_phase(ctx, "clone")

        # 4. Reuse a checkout that is already at the pinned commit. A resume
        # re-entered this tool on every turn and each entry wiped and re-fetched
        # the identical tree; a shallow checkout at the right sha is
        # indistinguishable from the one a re-clone would produce, so the network
        # round-trip buys nothing. Anything else — absent, wrong sha, not a repo —
        # re-clones.
        reused = False
        if pinned_sha is not None and _head_of(clone_dir) == pinned_sha:
            reused = True
            emit_log(ctx, f"Reusing existing clone at {pinned_sha[:7]}")
        else:
            outcome = self._acquire(ctx, args, pinned_sha=pinned_sha)
            if not outcome.ok:
                # ``outcome.stderr`` is already redacted of every candidate secret.
                ctx.store.append_job_event(ctx.job_id, {
                    "type": "error",
                    "error": {"code": "repo_access", "message": outcome.stderr},
                })
                ctx.store.update_job(ctx.job_id, status="failed")
                return _err_result("repo_access", outcome.stderr)

        # 5. Count files (skip .git internals).
        total = sum(
            1
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

        # 6. Resolve HEAD commit SHA + current branch. With ``--depth=1``
        # the working tree is a normal branch checkout (not detached), so
        # ``--abbrev-ref HEAD`` returns the branch name. A commit-pinned checkout
        # is detached, so the branch falls back to the ref the caller named and
        # then to the one the job already recorded — never to empty, which
        # ``update_job``'s merge would write over a good value.
        head = _git_rev_parse(clone_dir, ["HEAD"]) or ""
        branch = _git_rev_parse(clone_dir, ["--abbrev-ref", "HEAD"]) or ""
        if not branch or branch == "HEAD":
            branch = args.ref or (job.branch if job is not None else None) or ""

        # 7. GUARD the writeback. The pin used to erase its own record: whatever
        # HEAD the clone landed on was written straight back over ``commit_sha``,
        # so a pin that failed to take left no trace it had ever been requested —
        # the job then read as though it had always meant the new commit. A pinned
        # job therefore never REWRITES the sha, it only VERIFIES it, and a
        # mismatch is a hard failure rather than a new record.
        if pinned_sha is not None and head and head != pinned_sha:
            err = (
                f"clone is at {head} but this job is pinned to {pinned_sha} — "
                "refusing to overwrite the recorded commit"
            )
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error",
                "error": {"code": "repo_access", "message": err},
            })
            ctx.store.update_job(ctx.job_id, status="failed")
            return _err_result("repo_access", err)

        # 8. Update job record (commit + branch land here, not in finalize,
        # so the indexing screen can surface them mid-flight) and emit the
        # ``queued`` event. update_job MERGES on snake_case keys, so a pinned job
        # sends neither the commit (already correct, and verified above) nor an
        # empty branch — passing either would clobber a good value with None.
        fields: dict[str, Any] = {"status": "scanning", "total_count": total}
        if pinned_sha is None:
            fields["branch"] = branch or None
            fields["commit_sha"] = head or None
        elif branch:
            fields["branch"] = branch
        ctx.store.update_job(ctx.job_id, **fields)
        ctx.store.append_job_event(ctx.job_id, {
            "type": "queued",
            "jobId": ctx.job_id,
            "slug": ctx.slug,
            "totalCount": total,
        })
        emit_log(
            ctx,
            f"{'Reused' if reused else 'Cloned'} {total} files in {clone_dir.name}",
        )

        return MockSpeaker(content=str({
            "totalCount": total,
            "ref": pinned_sha or args.ref or "HEAD",
            "head": head,
            "branch": branch,
            "clone_dir": str(clone_dir),
            "reused": reused,
        }))

    @staticmethod
    def _acquire(
        ctx: Any, args: WikiCloneArgs, *, pinned_sha: str | None
    ) -> CloneOutcome:
        """Fetch this job's source through the durable credential chain.

        Two shapes, one executor. A pinned job needs a bare COMMIT, which
        ``git clone`` cannot express — ``--branch`` resolves its argument as a
        branch/tag name on the remote — so it takes :func:`clone_at_sha`.
        Everything else is the ordinary shallow clone of a branch, tag or the
        default HEAD. Both routes end up in :func:`run_git_with_chain`, which owns
        credential precedence (arg → durable repo/host store → ambient git
        credential → anonymous), the helper-disable + ``GIT_TERMINAL_PROMPT=0``
        env, and secret redaction, and emits via ``on_log`` which source
        authenticated plus a warning per stored scope the remote rejected.
        """

        def on_log(text: str, *, level: str = "info") -> None:
            emit_log(ctx, text, level=level)

        if pinned_sha is not None:
            emit_log(ctx, f"Cloning {args.url} at pinned commit {pinned_sha[:7]}…")
            return clone_at_sha(
                args.url,
                ctx.clone_dir,
                sha=pinned_sha,
                store=ctx.store,
                slug=ctx.slug,
                arg_token=args.token,
                on_log=on_log,
            )
        emit_log(ctx, f"Cloning {args.url}{f' @ {args.ref}' if args.ref else ''}…")
        return clone_with_fallback(
            args.url,
            ctx.clone_dir,
            ref=args.ref,
            store=ctx.store,
            slug=ctx.slug,
            arg_token=args.token,
            on_log=on_log,
        )


# ---------------------------------------------------------------------------
# Module-level helpers (reused by Tasks 2.3-2.5+)
# ---------------------------------------------------------------------------


_PRIVATE_TLDS = (".home", ".local", ".internal", ".lan", ".intranet", ".corp")


# ── Shared clone core (credential chain) ────────────────────────────────────

#: A winning candidate's ``source`` → the human log line announcing which
#: credential authenticated the clone (emitted through ``on_log``).
_SOURCE_LOG: dict[CredentialSource, str] = {
    CredentialSource.ARG: "Authenticated via provided token",
    CredentialSource.STORE_REPO: "Authenticated via stored repository credential",
    CredentialSource.STORE_HOST: "Authenticated via shared host credential",
    CredentialSource.AMBIENT: "Authenticated via ambient git credential",
    CredentialSource.ANONYMOUS: "Anonymous clone (no stored credential)",
}


class CloneOutcome(BaseModel):
    """Result of a credential-chain clone attempt.

    Deliberately minimal: ``ok`` plus ``stderr`` (the last attempt's git stderr,
    ALWAYS redacted of every candidate secret). Which credential authenticated
    and which stored scope the remote rejected are surfaced live via the clone's
    ``on_log`` callback inside :func:`run_git_with_chain`, so neither caller has
    ever needed to read them back off the outcome.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    stderr: str


class GitChainOutcome(BaseModel):
    """Result of running ONE git command across the whole credential chain.

    ``winner`` is the :class:`CredentialCandidate` that authenticated (``None``
    on failure) — freshness threads the winner's token into its follow-up
    compare-API call so it can't re-resolve a *different* (possibly revoked)
    token than the one that just worked. But see the second trap in the package
    CLAUDE.md: a git success proves the REPO was reachable, NOT that the winning
    credential is valid (a public repo serves a revoked token happily), so an
    authenticated REST call must still re-walk the chain itself. ``stdout`` is
    the decoded output of the winning command; ``stderr_redacted`` the last
    attempt's scrubbed stderr on failure.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    stdout: str
    stderr_redacted: str
    winner: CredentialCandidate | None = None


def _redact(text: str, secrets: list[str]) -> str:
    """Replace every non-empty *secret* substring in *text* with ``<redacted>``."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<redacted>")
    return text


def run_git_with_chain(
    store: Any,
    slug: str,
    url: str,
    build_argv: Callable[[str], list[str]],
    *,
    arg_token: str | None = None,
    timeout: int,
    on_log: Callable[..., None] | None = None,
    reset_dir: Path | None = None,
) -> GitChainOutcome:
    """Run one git command against *url*, trying each credential-chain candidate.

    The SINGLE credential-iterating subprocess executor — both
    :func:`clone_with_fallback` and ``freshness`` drive through it, so the
    hardened posture (helper-disable via *build_argv*, ``GIT_TERMINAL_PROMPT=0``
    via :func:`hardened_git_env`, per-candidate secret redaction, SSH temp-key
    lifecycle, auth-vs-network failure discrimination) lives in ONE place.

    Iterates ``resolve_chain(store, slug, arg_token=...)`` LAZILY: a token
    candidate is injected into *url* (``x-access-token`` or the credential's own
    username) and an ssh_key candidate drives ``GIT_SSH_COMMAND`` via a temp key
    file, then *build_argv* turns the authenticated URL into the full argv (the
    caller bakes in its subcommand + ref + private-host TLS carve-out). On a
    non-zero exit git stderr is redacted of every secret tried so far; an
    AUTH-class failure (:func:`is_auth_failure`) advances to the next candidate
    (a rejected STORED scope is warned via *on_log*), any other failure —
    network, bad ref, timeout — aborts immediately (don't burn valid credentials
    on a non-auth error). *reset_dir*, when given, is emptied before each attempt
    (git refuses a non-empty clone target; a prior candidate may have left a
    partial checkout). *on_log* (optional) receives the which-credential-won /
    which-scope-rejected telemetry so both callers emit it with no duplication.
    """

    def _log(text: str, *, level: str = "info") -> None:
        if on_log is not None:
            on_log(text, level=level)

    secrets: list[str] = []
    last_stderr = "git command failed"
    scope = CredentialScope.coerce(slug)

    for candidate in resolve_chain(store, slug, arg_token=arg_token):
        if candidate.credential is not None:
            # Accumulate incrementally: a given attempt's stderr can only echo
            # the credential injected for THAT attempt, so redacting against the
            # secrets tried so far is complete.
            secrets.append(candidate.credential.value)

        cmd = build_argv(_inject_token(url, candidate.token, candidate.username))
        ssh_key = candidate.ssh_key

        if reset_dir is not None:
            if reset_dir.exists():
                shutil.rmtree(reset_dir, ignore_errors=True)
            reset_dir.mkdir(parents=True, exist_ok=True)

        run_env, key_path = _ssh_env_for(ssh_key)
        env = hardened_git_env(run_env)
        try:
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                # A timeout is not an auth failure — abort the whole chain.
                return GitChainOutcome(
                    ok=False, stdout="",
                    stderr_redacted=f"git command timed out after {timeout}s",
                    winner=None,
                )
        finally:
            if key_path is not None:
                key_path.unlink(missing_ok=True)

        if proc.returncode == 0:
            _log(_SOURCE_LOG.get(candidate.source, "Authenticated"))
            stdout = (proc.stdout or b"").decode(errors="ignore")
            return GitChainOutcome(
                ok=True, stdout=stdout, stderr_redacted="", winner=candidate
            )

        stderr = (proc.stderr or b"").decode(errors="ignore").strip() or "git command failed"
        last_stderr = _redact(stderr, secrets)
        if is_auth_failure(last_stderr):
            # Only a STORED candidate names a scope the user can go fix; the
            # source itself knows which one (arg/ambient/anonymous → None).
            rejected = candidate.source.scope_for(scope) if scope is not None else None
            if rejected is not None:
                _log(
                    f"Stored credential for {rejected} was rejected by the remote — "
                    "update it in Settings → Git Credentials",
                    level="warning",
                )
            continue  # auth-class failure — try the next credential
        # Non-auth failure (network / bad ref) — don't iterate over the chain.
        return GitChainOutcome(ok=False, stdout="", stderr_redacted=last_stderr, winner=None)

    # Every candidate exhausted (all auth-rejected).
    return GitChainOutcome(ok=False, stdout="", stderr_redacted=last_stderr, winner=None)


def clone_with_fallback(
    url: str,
    clone_dir: Any,
    *,
    ref: str | None,
    store: Any,
    slug: str,
    arg_token: str | None = None,
    on_log: Callable[..., None] | None = None,
) -> CloneOutcome:
    """Clone *url* into *clone_dir* through the shared credential-chain executor.

    A thin adapter over :func:`run_git_with_chain`: it supplies the clone argv
    builder (``build_clone_command`` — helper-disable + private-host TLS + ref
    pin), the 300s-per-attempt timeout, and *clone_dir* as the ``reset_dir`` so
    every candidate starts from an empty target. Credential precedence,
    hardening, and redaction all live in the executor — both this tool and
    ``GraphOnlyIndexer._clone`` call through here.
    """
    private = _is_private_host(url)
    target = Path(str(clone_dir))
    outcome = run_git_with_chain(
        store,
        slug,
        url,
        lambda authed: build_clone_command(authed, target, ref=ref, private_host=private),
        arg_token=arg_token,
        timeout=300,
        on_log=on_log,
        reset_dir=target,
    )
    return CloneOutcome(ok=outcome.ok, stderr=outcome.stderr_redacted)


def clone_at_sha(
    url: str,
    clone_dir: Any,
    *,
    sha: str,
    store: Any,
    slug: str,
    arg_token: str | None = None,
    on_log: Callable[..., None] | None = None,
) -> CloneOutcome:
    """Check out ONE commit *sha* of *url* into *clone_dir*.

    ``git clone --branch`` resolves its argument as a branch or tag name on the
    REMOTE, so it can never accept a raw commit: the remote answers
    ``Remote branch <sha> not found in upstream origin`` and the pin is simply
    lost. The portable shape is ``init`` + a depth-1 ``fetch`` of that one object
    + ``checkout FETCH_HEAD``.

    Only the fetch touches the network, so only the fetch walks the credential
    chain — through the same :func:`run_git_with_chain` every other git call in
    this package uses. It is deliberately NOT given a ``reset_dir``: the executor
    empties that directory before each attempt, which would delete the ``.git``
    the fetch needs. The target is reset and initialised ONCE here instead, and
    re-fetching under the next candidate is harmless. Fetching the URL directly
    rather than configuring a named remote also keeps the injected token out of
    ``.git/config``.
    """
    target = Path(str(clone_dir))
    private = _is_private_host(url)

    # Start from an empty repo: a partial checkout from a previous attempt would
    # otherwise leave objects behind that make the outcome ambiguous.
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    if _run_local_git(target, ["init", "-q"]) is None:
        return CloneOutcome(ok=False, stderr=f"could not initialise a repo in {target}")

    outcome = run_git_with_chain(
        store,
        slug,
        url,
        lambda authed: build_fetch_sha_command(
            authed, target, sha=sha, private_host=private
        ),
        arg_token=arg_token,
        timeout=300,
        on_log=on_log,
    )
    if not outcome.ok:
        return CloneOutcome(ok=False, stderr=outcome.stderr_redacted)

    if _run_local_git(target, ["checkout", "-q", "FETCH_HEAD"]) is None:
        return CloneOutcome(ok=False, stderr=f"fetched {sha} but could not check it out")
    return CloneOutcome(ok=True, stderr="")


def build_clone_command(
    clone_url: str, clone_dir: Any, *, ref: str | None, private_host: bool
) -> list[str]:
    """Build the ``git clone --depth=1 [...]`` argv (shared by both clone paths).

    Self-hosted servers on private TLDs (e.g. git.example.home) typically use
    self-signed certs, so ``private_host`` inserts ``-c http.sslVerify=false`` —
    public hosts (github.com, gitlab.com, ...) still validate normally. A non-null
    *ref* pins a single BRANCH OR TAG via ``--branch <ref> --single-branch``; null
    clones the repo's default branch. A raw commit sha is NOT a valid *ref* here
    (the remote resolves the value as a branch name) — use :func:`clone_at_sha`.
    """
    cmd: list[str] = ["git", "clone", "--depth=1"]
    # Disable ANY git credential helper for this subprocess: the container
    # gitconfig sets ``credential.helper=store`` against a READ-ONLY, bind-mounted
    # ``~/.git-credentials``, so on an auth rejection git tries to erase the entry
    # and the rename() fails with EBUSY — masking the real auth error. We read the
    # ambient credential ourselves (``git credential fill``, read-only) and inject
    # it into the URL, so git never needs to touch the mounted file.
    cmd[1:1] = ["-c", "credential.helper="]
    if private_host:
        cmd[1:1] = ["-c", "http.sslVerify=false"]
    if ref:
        cmd += ["--branch", ref, "--single-branch"]
    cmd += [clone_url, str(clone_dir)]
    return cmd


def build_fetch_sha_command(
    fetch_url: str, clone_dir: Any, *, sha: str, private_host: bool
) -> list[str]:
    """Build the depth-1 ``git fetch <url> <sha>`` argv for a commit-pinned clone.

    Carries the SAME hardened posture as the other builders — the
    ``credential.helper=`` disable (see :func:`build_clone_command` for the EBUSY
    trap that exists for) and the private-TLD TLS carve-out — so the pinned path
    can never drift from the clone's. The commit is fetched from the URL rather
    than a configured remote, which keeps the injected token out of
    ``.git/config``.
    """
    cmd: list[str] = ["git", "-C", str(clone_dir), "-c", "credential.helper="]
    if private_host:
        cmd += ["-c", "http.sslVerify=false"]
    cmd += ["fetch", "--depth=1", fetch_url, sha]
    return cmd


def build_ls_remote_command(
    url: str, *refs: str, private_host: bool, symref: bool = False
) -> list[str]:
    """Build a hardened ``git ls-remote`` argv (shared by every ls-remote path).

    Carries the SAME ``-c credential.helper=`` helper-disable + private-host
    ``http.sslVerify=false`` carve-out as :func:`build_clone_command`, so branch
    listing, freshness, and the credential-validate route can never drift from
    the clone's hardened posture (the un-hardened branch lister was where the
    EBUSY-masks-auth incident still reproduced). *symref* adds ``--symref`` (the
    branch picker reads ``HEAD``'s symref to name the default branch); *refs* are
    the ref specs to query (``HEAD``, ``refs/heads/<x>``, ``refs/heads/*``).
    """
    cmd: list[str] = ["git", "-c", "credential.helper="]
    if private_host:
        cmd += ["-c", "http.sslVerify=false"]
    cmd += ["ls-remote"]
    if symref:
        cmd += ["--symref"]
    cmd += [url, *refs]
    return cmd


def hardened_git_env(ssh_env: dict[str, str] | None = None) -> dict[str, str]:
    """Return the subprocess env every credential-resolving git call must run in.

    ``GIT_TERMINAL_PROMPT=0`` merged over either the SSH-key env (from
    :func:`_ssh_env_for`) or the inherited process env — so git never blocks on
    an interactive credential prompt and never falls back to the mounted
    credential helper. The ONE place this env is assembled.
    """
    env = dict(ssh_env) if ssh_env is not None else dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _is_private_host(url: str) -> bool:
    """Return True when *url*'s host sits on a reserved/private-network TLD.

    Self-hosted Gitea/Gitlab instances on these TLDs almost always serve
    self-signed certs, so we skip TLS verification for them. Public hosts
    (github.com, gitlab.com, ...) still validate normally.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(suf) for suf in _PRIVATE_TLDS)


def _inject_token(url: str, token: str | None, username: str | None = None) -> str:
    """Return *url* with ``<username>:<token>@`` inserted before the host.

    *username* defaults to ``x-access-token`` (GitHub/Gitea/GitLab all accept it
    for token auth); a credential carrying its own ``username`` overrides it.
    """
    if not token:
        return url
    parsed = urlparse(url)
    user = username or "x-access-token"
    netloc = f"{quote(user, safe='')}:{quote(token, safe='')}@{parsed.hostname or ''}"
    if parsed.port:
        netloc += f":{parsed.port}"
    return urlunparse(parsed._replace(netloc=netloc))


def _ssh_env_for(ssh_key: str | None) -> tuple[dict[str, str] | None, Path | None]:
    """Build the subprocess env for an SSH-key clone, plus the temp key path.

    Returns ``(None, None)`` when there is no SSH key (token/anon path keeps
    the inherited env). Otherwise writes *ssh_key* to a private temp file
    (mode 0600), returns an env with ``GIT_SSH_COMMAND`` pointing at it, and the
    path so the caller can delete it in a ``finally``. ``accept-new`` trusts a
    first-seen host key without an interactive prompt (we clone ephemerally).
    """
    if not ssh_key:
        return None, None
    import os  # noqa: PLC0415
    import shlex  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    fd, name = tempfile.mkstemp(prefix="mewbo-wiki-key-")
    key_path = Path(name)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(ssh_key if ssh_key.endswith("\n") else ssh_key + "\n")
    key_path.chmod(0o600)
    env = dict(os.environ)
    # Quote the key path — TMPDIR can contain spaces, which would otherwise
    # split GIT_SSH_COMMAND mid-path and break the clone.
    env["GIT_SSH_COMMAND"] = (
        f"ssh -i {shlex.quote(str(key_path))} "
        "-o StrictHostKeyChecking=accept-new -o IdentitiesOnly=yes"
    )
    return env, key_path


def _run_local_git(clone_dir: Any, args: list[str]) -> str | None:
    """Run a LOCAL ``git -C <clone_dir> <args>`` and return its stripped stdout.

    Local means no network and therefore no credential: ``init``,
    ``checkout FETCH_HEAD``, ``rev-parse``. It still disables the credential
    helper and runs under :func:`hardened_git_env`, so no git invocation anywhere
    in this module can reach the read-only mounted credential file.

    ``None`` signals FAILURE only — a command that succeeds with no output (an
    ``init -q``) returns ``""``, so a caller can tell "it worked and said
    nothing" from "it did not work".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone_dir), "-c", "credential.helper=", *args],
            capture_output=True,
            timeout=30,
            env=hardened_git_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode(errors="ignore").strip()


def _git_rev_parse(clone_dir: Any, args: list[str]) -> str | None:
    """Run ``git -C <clone_dir> rev-parse <args>`` and return stdout.

    Returns the stripped stdout on success, ``None`` on any failure. Best-
    effort — callers must handle ``None`` (the wiki tolerates missing
    branch/commit metadata; the FE renders the snapshot block compactly).
    """
    return _run_local_git(clone_dir, ["rev-parse", *args]) or None


def _head_of(clone_dir: Any) -> str | None:
    """Return the commit an existing checkout sits at, or ``None``.

    ``None`` is the single answer for every "this directory cannot be reused"
    case — absent, empty, not a git repo, or unreadable — so the caller only has
    to compare it against the pin and re-clone on anything but an exact match.
    """
    target = Path(str(clone_dir))
    if not (target / ".git").exists():
        return None
    return _git_rev_parse(target, ["HEAD"])


__all__ = [
    "CloneOutcome",
    "GitChainOutcome",
    "WikiCloneArgs",
    "WikiCloneRepoTool",
    "build_clone_command",
    "build_fetch_sha_command",
    "build_ls_remote_command",
    "clone_at_sha",
    "clone_with_fallback",
    "hardened_git_env",
    "run_git_with_chain",
    "_inject_token",
    "_redact",
    "_ssh_env_for",
    "_is_private_host",
    "_err_result",
    "_git_rev_parse",
    "_head_of",
    "_run_local_git",
]
