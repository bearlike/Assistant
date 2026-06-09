"""``wiki_clone_repo`` SessionTool — deterministic git clone + queued event emission."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse, urlunparse

from mewbo_core.common import MockSpeaker, get_logger, pydantic_to_openai_tool
from pydantic import BaseModel, ConfigDict, Field

from mewbo_graph.plugins.wiki._base import WikiSessionTool, _err_result
from mewbo_graph.plugins.wiki._ctx import emit_log, emit_phase, resolve_runtime

if TYPE_CHECKING:
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
    ref: str | None = Field(default=None, description="Optional branch/tag/sha to checkout.")
    token: str | None = Field(default=None, description="Optional auth token (not persisted).")


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

        # 3. Resolve the credential. Order: LLM arg (fast) → ephemeral
        # CloneTokenCache (warm path) → durable CredentialStore (source of
        # truth — survives the process that warmed the cache). The SSH branch
        # authenticates via GIT_SSH_COMMAND + a temp key file; the token branch
        # injects x-access-token into the URL (never persisted).
        from mewbo_graph.wiki.credentials import CredentialStore  # noqa: PLC0415
        from mewbo_graph.wiki.tokens import CloneTokenCache  # noqa: PLC0415

        effective_token = args.token or CloneTokenCache.peek(ctx.job_id)
        ssh_key: str | None = None
        if not effective_token:
            cred = CredentialStore.load(ctx.store, ctx.slug)
            if cred is not None and cred.kind == "token":
                effective_token = cred.value
            elif cred is not None and cred.kind == "ssh_key":
                ssh_key = cred.value
        clone_url = _inject_token(args.url, effective_token)

        # 4. Prepare clone dir (clean if present from a prior partial run).
        clone_dir = ctx.clone_dir
        if clone_dir.exists():
            shutil.rmtree(clone_dir, ignore_errors=True)
        clone_dir.mkdir(parents=True, exist_ok=True)

        emit_phase(ctx, "clone")
        emit_log(ctx, f"Cloning {args.url}{f' @ {args.ref}' if args.ref else ''}…")

        # 5. Run git clone (shallow, one branch).
        cmd: list[str] = ["git", "clone", "--depth=1"]
        # Self-hosted servers on private TLDs (e.g. git.example.home) typically
        # use self-signed certs. Skip TLS verification for those hosts only —
        # public hosts (github.com, gitlab.com, ...) still validate normally.
        if _is_private_host(args.url):
            cmd[1:1] = ["-c", "http.sslVerify=false"]
        if args.ref:
            cmd += ["--branch", args.ref, "--single-branch"]
        cmd += [clone_url, str(clone_dir)]

        run_env, key_path = _ssh_env_for(ssh_key)
        try:
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=300, env=run_env)
            except subprocess.TimeoutExpired:
                err_msg = "git clone timed out after 300s"
                ctx.store.append_job_event(ctx.job_id, {
                    "type": "error",
                    "error": {"code": "repo_access", "message": err_msg},
                })
                ctx.store.update_job(ctx.job_id, status="failed")
                return _err_result("repo_access", err_msg)
        finally:
            if key_path is not None:
                key_path.unlink(missing_ok=True)

        if proc.returncode != 0:
            err_msg = (proc.stderr or b"").decode(errors="ignore").strip() or "git clone failed"
            # Scrub EVERY non-None secret that could be in the URL before it
            # reaches the event log OR the tool result. On the durable path the
            # real token is in ``effective_token`` (args.token is None), and an
            # SSH key value is a file path/content — none must leak verbatim.
            for secret in filter(None, [args.token, effective_token, ssh_key]):
                err_msg = err_msg.replace(secret, "<redacted>")
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error",
                "error": {"code": "repo_access", "message": err_msg},
            })
            ctx.store.update_job(ctx.job_id, status="failed")
            return _err_result("repo_access", err_msg)

        # 6. Count files (skip .git internals).
        total = sum(
            1
            for p in clone_dir.rglob("*")
            if p.is_file() and ".git" not in p.parts
        )

        # 7. Resolve HEAD commit SHA + current branch. With ``--depth=1``
        # the working tree is a normal branch checkout (not detached), so
        # ``--abbrev-ref HEAD`` returns the branch name. Detached HEAD
        # (e.g. when args.ref is a SHA) falls back to the requested ref.
        head = _git_rev_parse(clone_dir, ["HEAD"]) or ""
        branch = _git_rev_parse(clone_dir, ["--abbrev-ref", "HEAD"]) or ""
        if not branch or branch == "HEAD":
            branch = args.ref or ""

        # 8. Update job record (commit + branch land here, not in finalize,
        # so the indexing screen can surface them mid-flight) and emit the
        # ``queued`` event. update_job merges using snake_case keys.
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

        return MockSpeaker(content=str({
            "totalCount": total,
            "ref": args.ref or "HEAD",
            "head": head,
            "branch": branch,
            "clone_dir": str(clone_dir),
        }))


# ---------------------------------------------------------------------------
# Module-level helpers (reused by Tasks 2.3-2.5+)
# ---------------------------------------------------------------------------


_PRIVATE_TLDS = (".home", ".local", ".internal", ".lan", ".intranet", ".corp")


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


def _inject_token(url: str, token: str | None) -> str:
    """Return *url* with ``x-access-token:<token>@`` inserted before the host."""
    if not token:
        return url
    parsed = urlparse(url)
    netloc = f"x-access-token:{quote(token, safe='')}@{parsed.hostname or ''}"
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


def _git_rev_parse(clone_dir: Any, args: list[str]) -> str | None:
    """Run ``git -C <clone_dir> rev-parse <args>`` and return stdout.

    Returns the stripped stdout on success, ``None`` on any failure. Best-
    effort — callers must handle ``None`` (the wiki tolerates missing
    branch/commit metadata; the FE renders the snapshot block compactly).
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", *args],
            capture_output=True,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return (proc.stdout or b"").decode(errors="ignore").strip() or None


__all__ = [
    "WikiCloneArgs",
    "WikiCloneRepoTool",
    "_inject_token",
    "_ssh_env_for",
    "_err_result",
    "_git_rev_parse",
]
