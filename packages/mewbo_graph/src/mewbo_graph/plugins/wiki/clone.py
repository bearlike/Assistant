"""``wiki_clone_repo`` SessionTool — deterministic git clone + queued event emission."""
from __future__ import annotations

import shutil
import subprocess
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

        # 3. Build the clone URL with token rewriting (never persisted).
        # Prefer the LLM-supplied token, but fall back to the ephemeral
        # token the API stashed at submission time so the LLM never has
        # to see the secret.
        effective_token = args.token
        if not effective_token:
            from mewbo_graph.wiki.tokens import CloneTokenCache  # noqa: PLC0415

            effective_token = CloneTokenCache.peek(ctx.job_id)
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

        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=300)
        except subprocess.TimeoutExpired:
            err_msg = "git clone timed out after 300s"
            ctx.store.append_job_event(ctx.job_id, {
                "type": "error",
                "error": {"code": "repo_access", "message": err_msg},
            })
            ctx.store.update_job(ctx.job_id, status="failed")
            return _err_result("repo_access", err_msg)

        if proc.returncode != 0:
            err_msg = (proc.stderr or b"").decode(errors="ignore").strip() or "git clone failed"
            # Scrub token from any error message before persisting.
            if args.token:
                err_msg = err_msg.replace(args.token, "<redacted>")
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
    "_err_result",
    "_git_rev_parse",
]
