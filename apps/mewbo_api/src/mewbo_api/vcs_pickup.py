"""Agent pickup endpoint for VCS automation (GitHub / Gitea Actions).

``POST /api/automation/vcs-pickup`` is the CI sibling of the chat channel
adapters (``channels/``): an inbound platform event becomes a tagged session
that is created or continued, here keyed ``vcs:<owner/repo>:<kind>:<number>``
(cf. ``nextcloud-talk:room:<token>``). It differs from chat channels only
where the transport differs — auth is the API key (CI holds a secret; there
is no HMAC handshake). The reply leg mirrors the channels exactly: an
``on_session_end`` hook recovers the originating issue/PR from the session's
``vcs_pickup`` context event and posts the final answer back as a comment
authored by the bot account (token per forge host under ``channels.vcs`` in
config); side effects beyond that (pushes, opened PRs) travel through the
agent's own VCS tools.

:class:`VcsPickupService` owns the whole pickup behavior over its injected
collaborators: resolve the repository to a project (including never-promoted
config projects, matched by git remote identity), prepare an isolated worktree
— a PR's head branch (fetch + find-or-create), or a ``mewbo/issue-<n>`` branch
cut from HEAD for an issue, both surviving the session-end reaper — and enqueue
the prompt — steering an active run or starting a fresh one. Wired by
:func:`init_vcs_pickup` from ``backend.py`` (same DI pattern as
``ide_routes.py``).
"""

from __future__ import annotations

import json
import ssl
import subprocess
import urllib.request
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlparse

from flask import request
from flask_restx import Namespace, Resource, fields
from mewbo_core.common import get_logger
from mewbo_core.config import get_config, get_config_value
from mewbo_core.exit_plan_mode import session_temp_dir
from mewbo_core.permissions import auto_approve
from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mewbo_core.hooks import HookManager
    from mewbo_core.project_store import ProjectStoreBase, VirtualProject
    from mewbo_core.session_runtime import SessionRuntime

from mewbo_api.responses import ApiResponseKit

logging = get_logger(name="api.vcs_pickup")

vcs_ns = Namespace("automation", description="CI/VCS automation endpoints")

# One DRY home for this namespace's error examples (every vcs-pickup error path
# returns the legacy ``{"message": ...}`` shape). Built at module level so the
# import-time decorators can see it; ``Vcs`` prefix namespaces the generated
# model names on the shared Api registry.
kit = ApiResponseKit(vcs_ns, prefix="Vcs")

_pickup_model = vcs_ns.model(
    "VcsPickupRequest",
    {
        "repository": fields.String(
            required=True,
            description="owner/repo of the triggering repository.",
            example="acme/widgets",
        ),
        "kind": fields.String(
            required=True,
            enum=["issue", "pull_request"],
            description="Whether the trigger is an issue or a pull request.",
            example="pull_request",
        ),
        "number": fields.Integer(
            required=True,
            min=1,
            description="Issue or pull request number.",
            example=42,
        ),
        "provider": fields.String(
            description="Forge that sent the event. Informational.",
            example="gitea",
        ),
        "api_url": fields.String(
            description=(
                "Forge REST API base URL. Required for the final answer to be posted "
                "back to the issue or pull request as a comment."
            ),
            example="https://git.example.com/api/v1",
        ),
        "event": fields.String(
            description="Triggering event name.",
            example="issue_comment.created",
        ),
        "url": fields.String(
            description="Web URL of the issue or pull request.",
            example="https://git.example.com/acme/widgets/pulls/42",
        ),
        "title": fields.String(description="Issue or pull request title."),
        "body": fields.String(description="Issue or pull request description."),
        "comment": fields.String(
            description="Text of the mention comment that triggered the pickup, when present.",
        ),
        "comment_author": fields.String(description="Login of the comment author."),
        "assignee": fields.String(description="Login the item was assigned to."),
        "bot_login": fields.String(
            description="Configured bot login, used to ignore the bot's own comments.",
        ),
        "head_ref": fields.String(
            description=(
                "Pull request head branch. When set, the session runs in a worktree "
                "checked out on this branch."
            ),
            example="feature/fix-login",
        ),
        "base_ref": fields.String(
            description="Pull request base branch.",
            example="main",
        ),
        "project": fields.String(
            description="Project key override. Defaults to the repository.",
        ),
        "model": fields.String(
            description=(
                "Optional model override. Any configured LiteLLM model id; omit for "
                "the configured default."
            ),
            example="openai/gpt-5.4-nano",
        ),
        "mode": fields.String(
            enum=["plan", "act"],
            description="Agent mode for the run.",
        ),
        "prompt": fields.String(
            description="Full override of the generated agent prompt.",
        ),
    },
)

AuthResult = tuple[dict, int] | None
AuthGuard = Callable[[], AuthResult]
# Matches backend._resolve_repo_or_404(project_key, promote=...).
RepoResolver = Callable[..., tuple[Any, tuple[dict, int] | None]]

# Success-response models. ``example=`` drives Scalar's sample body; each field
# matches a key in ``VcsPickupService.handle``'s return shapes.
_pickup_started_model = vcs_ns.model(
    "VcsPickupStarted",
    {
        "session_id": fields.String(
            example="9e2d47c1f0",
            description="The tag-keyed session the run was started or continued on.",
        ),
        "session_tag": fields.String(
            example="vcs:acme/widgets:pull_request:42",
            description="Deterministic tag vcs:<owner/repo>:<kind>:<number>.",
        ),
        "run_id": fields.String(
            example="9e2d47c1f0:r1",
            description="Run handle for the started run (absent on a skipped trigger).",
        ),
        "resumed": fields.Boolean(
            example=False,
            description="True when an existing session for this item was continued.",
        ),
        "worktree_id": fields.String(
            example="b1c2d3e4",
            description="Managed worktree the run is bound to, or null for the main checkout.",
        ),
        "accepted": fields.Boolean(
            example=True, description="True when a fresh run was accepted and started."
        ),
        "enqueued": fields.Boolean(
            example=True,
            description="True (202 path) when the prompt was steered into an already-active run.",
        ),
        "skipped": fields.Boolean(
            example=True,
            description="True when the trigger was a no-op (e.g. the bot's own comment).",
        ),
        "reason": fields.String(
            example="comment author is the bot",
            description="Why the trigger was skipped (present only when skipped).",
        ),
    },
)


class VcsPickupBody(BaseModel):
    """Request body posted by the agent-pickup CI workflow."""

    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, description="owner/repo of the triggering repository")
    kind: Literal["issue", "pull_request"]
    number: int = Field(ge=1)
    provider: str | None = None  # "github" | "gitea" — informational
    api_url: str | None = None  # forge REST base, for posting the reply comment
    event: str | None = None  # e.g. "issues.assigned", "issue_comment.created"
    url: str | None = None
    title: str | None = None
    body: str | None = None
    comment: str | None = None  # the @mention comment text, when comment-triggered
    comment_author: str | None = None
    assignee: str | None = None
    bot_login: str | None = None  # configured bot login, for self-trigger suppression
    head_ref: str | None = None  # PR head branch
    base_ref: str | None = None  # PR base branch
    project: str | None = None  # override; defaults to ``repository``
    model: str | None = None
    mode: Literal["plan", "act"] | None = None
    prompt: str | None = None  # full override of the generated prompt


class VcsPickupService:
    """Turn one CI trigger into a branch-aware agent session.

    Atomic feature class: the injected collaborators are its state, the
    pickup pipeline (:meth:`handle`) and its git/worktree/prompt behaviors
    are its methods.
    """

    GIT_TIMEOUT_S = 120

    def __init__(
        self,
        runtime: SessionRuntime,
        resolve_repo: RepoResolver,
        project_store: ProjectStoreBase,
        hook_manager: HookManager | None,
    ) -> None:
        """Capture the injected collaborators as instance state."""
        self.runtime = runtime
        self.resolve_repo = resolve_repo
        self.project_store = project_store
        self.hook_manager = hook_manager

    # -- naming ------------------------------------------------------------

    @staticmethod
    def session_tag_for(repository: str, kind: str, number: int) -> str:
        """Deterministic session tag so one issue/PR maps to one conversation."""
        return f"vcs:{repository}:{kind}:{number}"

    # -- git plumbing --------------------------------------------------------

    @classmethod
    def _git(cls, cwd: str, *args: str) -> subprocess.CompletedProcess:
        """Run a git command, raising ``CalledProcessError`` on failure."""
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=cls.GIT_TIMEOUT_S,
            check=True,
        )

    @classmethod
    def _git_ok(cls, cwd: str, *args: str) -> bool:
        """Run a git command, returning success instead of raising."""
        try:
            cls._git(cwd, *args)
            return True
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            return False

    @classmethod
    def _ensure_local_branch(cls, repo_path: str, branch: str) -> None:
        """Fetch *branch* from origin and make sure a local ref exists.

        ``WorktreeManager.create`` (no ``base``) needs the branch to resolve,
        and the parent clone may have never seen a PR branch pushed after the
        last fetch. Failures are surfaced — a PR pickup without its branch is
        useless.
        """
        cls._git(repo_path, "fetch", "origin", branch)
        if not cls._git_ok(repo_path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            cls._git(repo_path, "branch", "--track", branch, f"origin/{branch}")

    def ensure_worktree(self, target: Any, branch: str) -> VirtualProject:
        """Find-or-create the managed worktree for *branch* under *target*.

        After creation/lookup the worktree is best-effort fast-forwarded so a
        resumed session picks up from where the remote branch left off.
        """
        self._ensure_local_branch(target.path, branch)
        wt = self.project_store.create_worktree(target.project_id, branch)
        # Sync an existing (or freshly created but behind) checkout. Best-effort:
        # a dirty worktree from an interrupted run must not block the pickup.
        if not self._git_ok(wt.path, "merge", "--ff-only", f"origin/{branch}"):
            logging.warning(
                "Worktree for branch '{}' could not fast-forward to origin; continuing as-is.",
                branch,
            )
        return wt

    @staticmethod
    def issue_branch_for(number: int) -> str:
        """Deterministic mewbo-owned branch backing an issue pickup worktree.

        Mewbo-owned (``mewbo/`` prefix) so the session-end reaper deletes the
        branch with the worktree (``ProjectStoreBase.delete_worktree``); shared
        per issue so repeated pickups land on the same isolated workspace.
        """
        return f"mewbo/issue-{number}"

    @classmethod
    def _default_base(cls, repo_path: str) -> str:
        """Resolve the freshest base ref for a new issue branch.

        Best-effort fetches origin and bases off its default branch
        (``origin/<default>``, the repo's HEAD); falls back to the parent
        clone's current ``HEAD`` when the remote default cannot be resolved
        (e.g. a clone with no ``origin/HEAD`` symref).
        """
        cls._git_ok(repo_path, "fetch", "origin")
        try:
            res = cls._git(repo_path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            ref = res.stdout.strip()
            if ref:
                return ref
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
            pass
        return "HEAD"

    def ensure_issue_worktree(self, target: Any, number: int) -> VirtualProject | None:
        """Best-effort isolated worktree from HEAD for an issue pickup.

        Cuts a deterministic ``mewbo/issue-<n>`` branch from the default-branch
        HEAD so the agent works in isolation (concurrent issue pickups never
        collide in a shared checkout) and gets a clean, push-ready branch to
        open a pull request from. Idempotent: a repeat pickup reuses the branch
        the first one cut (session continuity) rather than re-basing it.

        Returns ``None`` — and the caller falls back to the shared main
        checkout — when the project has no managed parent or the worktree
        cannot be created (e.g. a non-git project path). Unlike a PR's own
        branch, an isolated branch is a nicety for an issue, not a hard
        requirement, so failure degrades instead of erroring.
        """
        if not target.project_id:
            return None
        branch = self.issue_branch_for(number)
        # Only cut a fresh branch from HEAD the first time; a later pickup
        # reuses the existing branch so the agent resumes where it left off.
        base: str | None = None
        if not self._git_ok(target.path, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
            base = self._default_base(target.path)
        try:
            return self.project_store.create_worktree(target.project_id, branch, base=base)
        except Exception as exc:  # noqa: BLE001 - best-effort; fall back to main checkout
            logging.warning(
                "Issue #{} worktree could not be prepared ({}); using main checkout.",
                number,
                exc,
            )
            return None

    # -- project resolution --------------------------------------------------

    @staticmethod
    def _config_project_for_repo(repo_key: str) -> str | None:
        """Match *repo_key* (e.g. ``owner/repo``) against config projects' remotes.

        ``_resolve_repo_or_404``'s identity matching only scans *managed*
        projects, so a config-defined project that was never promoted does not
        resolve by its ``owner/repo`` identity. CI sends exactly that key.
        """
        from mewbo_api.repo_identity import RepoIdentity

        for name, cfg in get_config().projects.items():
            path = getattr(cfg, "path", None)
            if not path:
                continue
            try:
                if repo_key in RepoIdentity.aliases_for_path(path):
                    return name
            except Exception:  # pragma: no cover - unreadable project dir
                continue
        return None

    def resolve_target(
        self, body: VcsPickupBody, *, promote: bool
    ) -> tuple[Any, tuple[dict, int] | None]:
        """Resolve the request's project key, falling back to git identity."""
        project_key = (body.project or body.repository).strip()
        target, err = self.resolve_repo(project_key, promote=promote)
        if err and not body.project:
            config_name = self._config_project_for_repo(project_key)
            if config_name:
                target, err = self.resolve_repo(config_name, promote=promote)
        return target, err

    # -- reply delivery --------------------------------------------------------

    # Both forges reject comment bodies past 64 KiB; leave headroom for the
    # truncation marker.
    COMMENT_MAX_CHARS = 60_000

    def post_comment(self, api_url: str, repository: str, number: int, text: str) -> bool:
        """Post *text* as an issue/PR comment authored by the bot account.

        ``POST /repos/{owner}/{repo}/issues/{n}/comments`` and the
        ``Authorization: token`` scheme are identical on GitHub and Gitea, so
        one client covers both providers. The token is looked up by forge
        host under ``channels.vcs.tokens`` in config; without one the reply
        leg is silently disabled (the pickup itself still works).
        """
        cfg = get_config().channels.get("vcs", {})
        token = (cfg.get("tokens") or {}).get(urlparse(api_url).hostname or "")
        if not token:
            logging.info("No channels.vcs token for {}; skipping reply comment.", api_url)
            return False
        if len(text) > self.COMMENT_MAX_CHARS:
            text = text[: self.COMMENT_MAX_CHARS] + "\n\n*[truncated]*"
        req = urllib.request.Request(
            f"{api_url.rstrip('/')}/repos/{repository}/issues/{number}/comments",
            data=json.dumps({"body": text}).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"token {token}",
            },
        )
        ssl_ctx = ssl.create_default_context()
        if cfg.get("tls_verify") is False:  # opt-out for untrusted internal CAs
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE
        try:
            with urllib.request.urlopen(req, timeout=30, context=ssl_ctx) as resp:  # noqa: S310
                if resp.status in (200, 201):
                    logging.info("Posted pickup reply to {} #{}", repository, number)
                    return True
                logging.warning(
                    "Unexpected status {} posting reply to {} #{}",
                    resp.status,
                    repository,
                    number,
                )
        except Exception as exc:
            logging.warning("Failed to post reply to {} #{}: {}", repository, number, exc)
        return False

    def completion_hook(self, session_id: str, error: str | None = None) -> None:
        """``on_session_end`` hook: deliver the final answer to the issue/PR.

        CI counterpart of ``channels.routes._channel_completion_hook`` — the
        reply target is recovered from the session's latest ``vcs_pickup``
        context event, so it survives steering, resumes, and restarts.
        """
        from mewbo_api.channels.routes import extract_final_answer

        events = self.runtime.session_store.load_transcript(session_id)
        ctx: dict[str, Any] | None = None
        for event in reversed(events):
            payload = event.get("payload", {}) if event.get("type") == "context" else {}
            if payload.get("vcs_pickup"):
                ctx = dict(payload["vcs_pickup"])
                break
        if not ctx or not ctx.get("api_url"):
            return  # Not a pickup session, or workflow predates the reply leg.
        text = extract_final_answer(events, error)
        if not text:
            return
        self.post_comment(str(ctx["api_url"]), str(ctx["repository"]), int(ctx["number"]), text)

    # -- prompt ----------------------------------------------------------------

    @staticmethod
    def build_prompt(body: VcsPickupBody, *, worktree_branch: str | None = None) -> str:
        """Render the pickup prompt handed to the agent as its user query.

        *worktree_branch* is the branch the session's isolated worktree is
        checked out on (a PR head, or the ``mewbo/issue-<n>`` branch cut for an
        issue pickup). When ``None`` the issue ran in the shared main checkout
        and the agent is told to cut its own branch.
        """
        if body.prompt:
            return body.prompt
        kind_label = "pull request" if body.kind == "pull_request" else "issue"
        lines = [
            f"You were triggered as @{body.bot_login or body.assignee or 'the agent bot'} "
            f"on a {kind_label} ({body.event or 'manual dispatch'}).",
            "",
            f"Repository: {body.repository}",
            f"{kind_label.capitalize()} #{body.number}: {body.title or '(no title)'}",
        ]
        if body.url:
            lines.append(f"URL: {body.url}")
        if body.head_ref:
            lines.append(f"Branch: {body.head_ref} (base: {body.base_ref or 'default'})")
        elif worktree_branch:
            lines.append(f"Working branch: {worktree_branch} (cut from the default branch)")
        if body.body:
            lines += ["", f"{kind_label.capitalize()} description:", body.body]
        if body.comment:
            author = f"@{body.comment_author}" if body.comment_author else "a user"
            lines += ["", f"Comment from {author} that triggered you:", body.comment]
        lines += [
            "",
            "Instructions:",
            "- Read the project instructions and hydrate context before editing.",
        ]
        if body.kind == "pull_request":
            lines += [
                "- Your working directory is a worktree checked out on the branch "
                "above; continue from its current state.",
                "- Implement what is asked, run focused tests, then commit and push "
                "to this branch so the pull request updates.",
            ]
        elif worktree_branch:
            lines += [
                f"- Your working directory is an isolated worktree on branch "
                f"`{worktree_branch}`, cut fresh from the default branch. Commit "
                "your work there; never touch the default branch.",
                f"- Implement what is asked, run focused tests, then push "
                f"`{worktree_branch}` and open a pull request that references this "
                "issue if you have tools to do so.",
            ]
        else:
            lines += [
                "- Your working directory is the repository's main checkout. Create "
                "a feature branch or worktree for your changes; never commit "
                "directly to the default branch.",
                "- Implement what is asked, run focused tests, then push your branch "
                "and open a pull request that references this issue if you have "
                "tools to do so.",
            ]
        lines += [
            "- If the trigger is a question rather than a task, just answer it; "
            "do not make changes.",
            f"- Your final response is posted back to the {kind_label} as a "
            "comment, so make it a self-contained summary addressed to the "
            "people on the thread.",
        ]
        return "\n".join(lines)

    # -- pipeline ----------------------------------------------------------------

    def handle(self, body: VcsPickupBody) -> tuple[dict, int]:
        """Resolve project + branch, then start or continue the tagged session."""
        # Self-trigger suppression: the bot commenting on its own thread must
        # not spawn another run (the workflow also guards; defense in depth).
        if body.bot_login and body.comment_author == body.bot_login:
            return {"skipped": True, "reason": "comment author is the bot"}, 200

        # PR pickups bind to a worktree on the (required) head branch; issue
        # pickups get an isolated worktree cut from HEAD so the agent never
        # works in the shared main checkout (#72 expanded intent). Both need a
        # managed parent → promote on resolution.
        needs_pr_worktree = body.kind == "pull_request" and bool(body.head_ref)
        needs_worktree = needs_pr_worktree or body.kind == "issue"
        target, err = self.resolve_target(body, promote=needs_worktree)
        if err:
            return err
        assert target is not None

        worktree_id: str | None = None
        agent_branch: str | None = None
        issue_wt = (
            self.ensure_issue_worktree(target, body.number) if body.kind == "issue" else None
        )
        if needs_pr_worktree and body.head_ref:
            try:
                wt = self.ensure_worktree(target, body.head_ref)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or "").strip()
                return {
                    "message": f"Failed to prepare branch '{body.head_ref}': {detail}"
                }, 422
            except (KeyError, ValueError, RuntimeError, OSError) as exc:
                return {
                    "message": f"Failed to prepare worktree for '{body.head_ref}': {exc}"
                }, 422
            worktree_id = wt.project_id
            project_ref = f"managed:{wt.project_id}"
            cwd = wt.path
            agent_branch = body.head_ref
        elif issue_wt is not None:
            worktree_id = issue_wt.project_id
            project_ref = f"managed:{issue_wt.project_id}"
            cwd = issue_wt.path
            agent_branch = issue_wt.branch
        elif target.project_id:
            project_ref = f"managed:{target.project_id}"
            cwd = target.path
        else:
            project_ref = target.name
            cwd = target.path

        tag = self.session_tag_for(body.repository, body.kind, body.number)
        existing = self.runtime.session_store.resolve_tag(tag)
        session_id = self.runtime.resolve_session(session_tag=tag)
        prompt = self.build_prompt(body, worktree_branch=agent_branch)

        # A run already in flight for this item → steer it instead of 409ing.
        if self.runtime.is_running(session_id) and self.runtime.enqueue_message(
            session_id, prompt
        ):
            return {
                "session_id": session_id,
                "session_tag": tag,
                "enqueued": True,
                "resumed": True,
            }, 202

        model = body.model or get_config_value("llm", "default_model", default="unknown")
        context_payload: dict[str, object] = {
            "project": project_ref,
            "model": model,
            "origin": "channel",
            "vcs_pickup": {
                "provider": body.provider,
                "api_url": body.api_url,
                "repository": body.repository,
                "event": body.event,
                "kind": body.kind,
                "number": body.number,
                "url": body.url,
            },
        }
        if agent_branch:
            context_payload["branch"] = agent_branch
        if body.mode:
            context_payload["mode"] = body.mode
        self.runtime.append_context_event(session_id, context_payload)

        budget = int(get_config_value("agent", "session_step_budget", default=0))
        max_iters = int(get_config_value("agent", "max_iters", default=30))
        # Surface = the originating forge; derive from the api_url host so the
        # trace knows github.com vs a self-hosted Gitea (the default, Gitea-first).
        host = (urlparse(body.api_url).hostname or "") if body.api_url else ""
        is_github = host == "github.com" or host.endswith(".github.com")
        source_platform = "github" if is_github else "gitea"
        run_id = self.runtime.start_async(
            session_id=session_id,
            user_query=prompt,
            model_name=str(model) or None,
            approval_callback=auto_approve,
            hook_manager=self.hook_manager,
            mode=body.mode,
            cwd=cwd or session_temp_dir(session_id),
            max_iters=max_iters,
            session_step_budget=budget,
            source_platform=source_platform,
        )
        if not run_id:
            return {"message": "Session is already running."}, 409
        logging.info(
            "vcs-pickup started run {} for {} #{} (session {}, tag {})",
            run_id,
            body.repository,
            body.number,
            session_id,
            tag,
        )
        return {
            "session_id": session_id,
            "session_tag": tag,
            "run_id": run_id,
            "resumed": existing is not None,
            "worktree_id": worktree_id,
            "accepted": True,
        }, 200


def _no_auth() -> AuthResult:
    return None


# Populated by ``init_vcs_pickup`` at app startup.
_service: VcsPickupService | None = None
_require_api_key: AuthGuard = _no_auth


def init_vcs_pickup(
    runtime: SessionRuntime,
    require_api_key: AuthGuard,
    resolve_repo: RepoResolver,
    project_store: ProjectStoreBase,
    hook_manager: HookManager,
) -> None:
    """Wire the namespace to its collaborators (called once at app startup)."""
    global _service, _require_api_key
    _require_api_key = require_api_key
    _service = VcsPickupService(runtime, resolve_repo, project_store, hook_manager)
    # Reply leg: post the final answer back to the issue/PR when a run ends
    # (same mechanism as the chat channels' completion hook).
    hook_manager.on_session_end.append(_service.completion_hook)


@vcs_ns.route("/automation/vcs-pickup")
class VcsPickup(Resource):
    """Start or continue an agent session for an assigned/mentioned issue or PR."""

    @vcs_ns.doc(
        security="apikey",
        description=(
            "Start or continue an agent session for an issue or pull-request event "
            "from CI (GitHub / Gitea Actions). Sessions are keyed by the "
            "deterministic tag `vcs:<owner/repo>:<kind>:<number>`, so repeated "
            "triggers for the same item reuse one conversation.\n\n"
            "**Outcomes:**\n\n"
            "- `200` — a fresh run was started (`accepted: true`, with `run_id`), or "
            "the trigger was a no-op (`skipped: true`, e.g. the bot's own comment).\n"
            "- `202` — a run was already active, so the prompt was enqueued as a "
            "steering message (`enqueued: true`).\n\n"
            "Pull-request pickups with a `head_ref` run in a worktree checked out on "
            "that branch; issue pickups get an isolated `mewbo/issue-<n>` worktree. "
            "When a forge token is configured server-side (`channels.vcs.tokens`), "
            "the agent's final answer is posted back to the issue/PR as a comment."
        ),
    )
    @vcs_ns.expect(_pickup_model)
    @vcs_ns.response(
        200, "Run started, or trigger skipped (e.g. the bot's own comment)", _pickup_started_model
    )
    @vcs_ns.response(
        202,
        "Prompt enqueued as a steering message into the already-active run",
        _pickup_started_model,
    )
    @kit.errors(404, 409, 422, shape="message")
    @kit.errors(400, shape="message")
    @kit.auth_error()
    def post(self) -> tuple[dict, int]:
        """Trigger an agent pickup.

        Starts or continues an agent session for an issue or pull request event
        from CI. Sessions are keyed by the deterministic tag
        `vcs:<owner/repo>:<kind>:<number>`, so repeated triggers for the same item
        reuse one conversation. If a run is already active on that session, the
        new prompt is enqueued as a steering message and the request returns 202.
        Pull request pickups with a `head_ref` run in a worktree checked out on
        that branch. When a forge token is configured server-side, the final
        answer is posted back to the issue or pull request as a comment.
        """
        assert _service is not None
        auth_error = _require_api_key()
        if auth_error:
            return auth_error
        try:
            body = VcsPickupBody.model_validate(request.get_json(silent=True) or {})
        except ValidationError as exc:
            return {"message": f"Invalid input: {exc.errors(include_url=False)}"}, 400
        return _service.handle(body)
