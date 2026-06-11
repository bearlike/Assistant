"""Tests for ``POST /api/automation/vcs-pickup`` (CI agent-pickup endpoint).

Covers: auth + body validation, self-trigger suppression, issue pickup
(cwd + deterministic session tag + context payload), PR pickup (real
``_ensure_worktree`` against a bare-origin temp git repo AND a stubbed
variant), continuity (tag-resolved session reuse + steering enqueue while
running), prompt content/override, and error paths (resolver 404, git 422,
start_async refusal 409).

Stubs ONLY the I/O boundaries: the ``mewbo_api.vcs_pickup`` module globals
(``_runtime`` / ``_resolve_repo`` / ``_project_store``) via monkeypatch
(auto-restored), keeping the real Flask route, Pydantic validation, and
prompt builder intact. backend.py's import-time ``init_vcs_pickup`` wiring is
relied on — the namespace is never re-registered (global-state leak rule).
"""

# mypy: ignore-errors

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

if shutil.which("git") is None:  # pragma: no cover
    pytest.skip("git not installed", allow_module_level=True)

from mewbo_api import backend, vcs_pickup

URL = "/api/automation/vcs-pickup"


# ---------------------------------------------------------------------------
# Fakes (simple classes capturing calls — not over-mocked)
# ---------------------------------------------------------------------------


class FakeSessionStore:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.transcript: list[dict] = []

    def resolve_tag(self, tag: str) -> str | None:
        return self.tags.get(tag)

    def load_transcript(self, session_id: str) -> list[dict]:
        return self.transcript


class FakeRuntime:
    """Captures start_async kwargs and models tag-based session continuity."""

    def __init__(self, *, running: bool = False) -> None:
        self.session_store = FakeSessionStore()
        self.running = running
        self.start_calls: list[dict] = []
        self.context_events: list[tuple[str, dict]] = []
        self.enqueued: list[tuple[str, str]] = []
        self.start_result: str | None = None  # None → mint "<sid>:r<n>"
        self._counter = 0

    def resolve_session(self, session_tag: str | None = None, **_kw) -> str:
        sid = self.session_store.tags.get(session_tag or "")
        if sid is None:
            self._counter += 1
            sid = f"sess-{self._counter}"
            if session_tag:
                self.session_store.tags[session_tag] = sid
        return sid

    def is_running(self, session_id: str) -> bool:
        return self.running

    def enqueue_message(self, session_id: str, text: str) -> bool:
        self.enqueued.append((session_id, text))
        return True

    def append_context_event(self, session_id: str, payload: dict) -> None:
        self.context_events.append((session_id, payload))

    def start_async(self, **kwargs) -> str:
        self.start_calls.append(kwargs)
        if self.start_result is not None:
            return self.start_result
        return f"{kwargs['session_id']}:r{len(self.start_calls)}"


def _target(project_id: str | None = "proj-1", name: str = "myrepo", path: str = "/repo"):
    return SimpleNamespace(project_id=project_id, name=name, path=path)


@pytest.fixture
def fake_runtime(monkeypatch) -> FakeRuntime:
    rt = FakeRuntime()
    monkeypatch.setattr(vcs_pickup._service, "runtime", rt)
    return rt


@pytest.fixture
def resolver_calls(monkeypatch, tmp_path: Path) -> list[dict]:
    """Stub the repo resolver with a managed target rooted at tmp_path."""
    calls: list[dict] = []
    target = _target(path=str(tmp_path))

    def fake_resolve(key, promote=False):
        calls.append({"key": key, "promote": promote})
        return target, None

    monkeypatch.setattr(vcs_pickup._service, "resolve_repo", fake_resolve)
    return calls


def _issue_body(**overrides) -> dict:
    body = {
        "repository": "acme/widget",
        "kind": "issue",
        "number": 7,
        "provider": "github",
        "event": "issues.assigned",
        "url": "https://github.com/acme/widget/issues/7",
        "title": "Fix the flux capacitor",
        "body": "It overheats at 88mph.",
        "bot_login": "mewbo-ai",
    }
    body.update(overrides)
    return body


def _pr_body(**overrides) -> dict:
    body = _issue_body(
        kind="pull_request",
        number=12,
        event="pull_request.assigned",
        title="Add cooling",
        head_ref="feature/cooling",
        base_ref="main",
    )
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# Auth + validation
# ---------------------------------------------------------------------------


def test_requires_api_key(client) -> None:
    resp = client.post(URL, json=_issue_body())
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "mutation",
    [
        {"unexpected_field": "boom"},  # extra="forbid"
        {"kind": "discussion"},  # not issue|pull_request
        {"number": 0},  # ge=1
        {"repository": ""},  # min_length=1
    ],
)
def test_invalid_body_returns_400(client, auth_headers, mutation) -> None:
    resp = client.post(URL, headers=auth_headers, json=_issue_body(**mutation))
    assert resp.status_code == 400
    assert "Invalid input" in resp.get_json()["message"]


def test_missing_body_returns_400(client, auth_headers) -> None:
    resp = client.post(URL, headers=auth_headers, json={})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Self-trigger suppression
# ---------------------------------------------------------------------------


def test_self_comment_is_skipped(client, auth_headers, fake_runtime, resolver_calls) -> None:
    resp = client.post(
        URL,
        headers=auth_headers,
        json=_issue_body(comment="done!", comment_author="mewbo-ai", bot_login="mewbo-ai"),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["skipped"] is True
    # Nothing downstream ran: no resolve, no session, no run.
    assert resolver_calls == []
    assert fake_runtime.start_calls == []


def test_other_author_comment_is_not_skipped(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    resp = client.post(
        URL,
        headers=auth_headers,
        json=_issue_body(comment="@mewbo-ai please fix", comment_author="alice"),
    )
    assert resp.status_code == 200
    assert resp.get_json().get("skipped") is None
    assert len(fake_runtime.start_calls) == 1


# ---------------------------------------------------------------------------
# Issue pickup
# ---------------------------------------------------------------------------


def test_issue_pickup_starts_async_run(
    client, auth_headers, fake_runtime, resolver_calls, tmp_path
) -> None:
    resp = client.post(
        URL,
        headers=auth_headers,
        json=_issue_body(comment="@mewbo-ai please fix", comment_author="alice"),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["accepted"] is True
    assert body["resumed"] is False
    assert body["session_tag"] == "vcs:acme/widget:issue:7"
    assert body["session_id"]
    assert body["run_id"]
    assert body["worktree_id"] is None

    # Resolver consulted with the repository key, no worktree promotion.
    assert resolver_calls == [{"key": "acme/widget", "promote": False}]

    # The run is bound to the project checkout.
    [call] = fake_runtime.start_calls
    assert call["session_id"] == body["session_id"]
    assert call["cwd"] == str(tmp_path)

    # Context event carries the project ref + vcs provenance, no branch.
    [(ctx_sid, ctx)] = fake_runtime.context_events
    assert ctx_sid == body["session_id"]
    assert ctx["project"] == "managed:proj-1"
    assert ctx["origin"] == "channel"
    assert ctx["vcs_pickup"]["repository"] == "acme/widget"
    assert ctx["vcs_pickup"]["kind"] == "issue"
    assert ctx["vcs_pickup"]["number"] == 7
    assert "branch" not in ctx

    # Prompt content: repository, number, title, body, comment + author,
    # and the issue-flavoured branch instruction.
    prompt = call["user_query"]
    assert "acme/widget" in prompt
    assert "#7" in prompt
    assert "Fix the flux capacitor" in prompt
    assert "It overheats at 88mph." in prompt
    assert "@mewbo-ai please fix" in prompt
    assert "@alice" in prompt
    assert "never commit directly to the default branch" in prompt
    assert "commit and push to this branch" not in prompt


def test_issue_pickup_project_override_resolves_that_key(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    resp = client.post(URL, headers=auth_headers, json=_issue_body(project="OtherProject"))
    assert resp.status_code == 200
    assert resolver_calls == [{"key": "OtherProject", "promote": False}]
    # Session tag stays keyed on the repository, not the project override.
    assert resp.get_json()["session_tag"] == "vcs:acme/widget:issue:7"


def test_prompt_override_is_used_verbatim(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    resp = client.post(
        URL, headers=auth_headers, json=_issue_body(prompt="Just say hello.")
    )
    assert resp.status_code == 200
    [call] = fake_runtime.start_calls
    assert call["user_query"] == "Just say hello."


def test_resolver_error_propagates(client, auth_headers, fake_runtime, monkeypatch) -> None:
    monkeypatch.setattr(
        vcs_pickup._service,
        "resolve_repo",
        lambda key, promote=False: (None, ({"message": f"Project '{key}' not found"}, 404)),
    )
    resp = client.post(URL, headers=auth_headers, json=_issue_body())
    assert resp.status_code == 404
    assert fake_runtime.start_calls == []


def test_start_async_refusal_returns_409(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    fake_runtime.start_result = ""  # registry refused a concurrent start
    resp = client.post(URL, headers=auth_headers, json=_issue_body())
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Continuity: same item → same session; running session → steering message
# ---------------------------------------------------------------------------


def test_second_pickup_resumes_same_session(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    first = client.post(URL, headers=auth_headers, json=_issue_body())
    assert first.get_json()["resumed"] is False

    second = client.post(
        URL, headers=auth_headers, json=_issue_body(comment="any update?", comment_author="alice")
    )
    assert second.status_code == 200
    body = second.get_json()
    assert body["resumed"] is True
    assert body["session_id"] == first.get_json()["session_id"]
    # Idle session → a fresh run, not a steering message.
    assert len(fake_runtime.start_calls) == 2


def test_running_session_gets_steering_message(
    client, auth_headers, fake_runtime, resolver_calls
) -> None:
    client.post(URL, headers=auth_headers, json=_issue_body())
    fake_runtime.running = True

    resp = client.post(
        URL, headers=auth_headers, json=_issue_body(comment="also add tests", comment_author="bob")
    )
    assert resp.status_code == 202
    body = resp.get_json()
    assert body["enqueued"] is True
    assert body["resumed"] is True
    assert "run_id" not in body

    [(sid, text)] = fake_runtime.enqueued
    assert sid == body["session_id"]
    assert "also add tests" in text
    # No second run was started.
    assert len(fake_runtime.start_calls) == 1


# ---------------------------------------------------------------------------
# PR pickup — stubbed worktree path
# ---------------------------------------------------------------------------


def test_pr_pickup_uses_worktree_context(
    client, auth_headers, fake_runtime, resolver_calls, monkeypatch, tmp_path
) -> None:
    wt = SimpleNamespace(project_id="wt:proj-1:feature-cooling", path=str(tmp_path / "wt"))
    ensured: list[tuple[str, str]] = []

    def fake_ensure_worktree(target, branch):
        ensured.append((target.project_id, branch))
        return wt

    monkeypatch.setattr(vcs_pickup._service, "ensure_worktree", fake_ensure_worktree)

    resp = client.post(URL, headers=auth_headers, json=_pr_body())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["accepted"] is True
    assert body["worktree_id"] == wt.project_id
    assert body["session_tag"] == "vcs:acme/widget:pull_request:12"

    # Resolver was asked to promote (worktrees need a managed parent).
    assert resolver_calls == [{"key": "acme/widget", "promote": True}]
    assert ensured == [("proj-1", "feature/cooling")]

    # Run cwd is the WORKTREE path and the context points at it + the branch.
    [call] = fake_runtime.start_calls
    assert call["cwd"] == wt.path
    [(_sid, ctx)] = fake_runtime.context_events
    assert ctx["project"] == f"managed:{wt.project_id}"
    assert ctx["branch"] == "feature/cooling"

    # PR-flavoured prompt: branch line + push-to-branch instruction.
    prompt = call["user_query"]
    assert "Branch: feature/cooling (base: main)" in prompt
    assert "commit and push" in prompt
    assert "never commit directly to the default branch" not in prompt


def test_pr_without_head_ref_falls_back_to_repo_checkout(
    client, auth_headers, fake_runtime, resolver_calls, tmp_path
) -> None:
    resp = client.post(URL, headers=auth_headers, json=_pr_body(head_ref=None))
    assert resp.status_code == 200
    assert resp.get_json()["worktree_id"] is None
    assert resolver_calls == [{"key": "acme/widget", "promote": False}]
    [call] = fake_runtime.start_calls
    assert call["cwd"] == str(tmp_path)


def test_pr_pickup_git_failure_returns_422(
    client, auth_headers, fake_runtime, resolver_calls, monkeypatch
) -> None:
    def boom(target, branch):
        raise subprocess.CalledProcessError(
            128, ["git", "fetch"], stderr="fatal: couldn't find remote ref"
        )

    monkeypatch.setattr(vcs_pickup._service, "ensure_worktree", boom)
    resp = client.post(URL, headers=auth_headers, json=_pr_body())
    assert resp.status_code == 422
    assert "feature/cooling" in resp.get_json()["message"]
    assert "couldn't find remote ref" in resp.get_json()["message"]
    assert fake_runtime.start_calls == []


# ---------------------------------------------------------------------------
# PR pickup — REAL _ensure_worktree against a bare origin + clone
# ---------------------------------------------------------------------------


def _git(repo: str, *args: str) -> None:
    subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, check=True)


@pytest.fixture
def cloned_project(tmp_path: Path):
    """A managed project whose checkout has a bare origin carrying a pushed
    PR branch that the clone has never fetched locally (the realistic shape
    ``_ensure_local_branch`` exists for)."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(str(seed), "init", "-b", "main")
    _git(str(seed), "config", "user.email", "t@e.com")
    _git(str(seed), "config", "user.name", "t")
    (seed / "README.md").write_text("hi\n")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "init")
    _git(str(seed), "checkout", "-b", "feature/pr-1")
    (seed / "fix.txt").write_text("fix\n")
    _git(str(seed), "add", "-A")
    _git(str(seed), "commit", "-m", "pr work")
    _git(str(seed), "checkout", "main")

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "clone", "--bare", str(seed), str(origin)],
        capture_output=True, text=True, check=True,
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "clone", str(origin), str(clone)],
        capture_output=True, text=True, check=True,
    )
    # The clone only has origin/feature/pr-1 — no local branch yet.

    proj = backend.project_store.create_project(
        name="vcs-pickup-real", description="", path=str(clone)
    )
    yield proj
    try:
        for wt in backend.project_store.list_worktrees(proj.project_id):
            try:
                backend.project_store.delete_worktree(wt.project_id, force=True)
            except Exception:
                pass
        backend.project_store.delete_project(proj.project_id)
    except Exception:
        pass


def test_pr_pickup_real_worktree_from_bare_origin(
    client, auth_headers, fake_runtime, cloned_project, monkeypatch
) -> None:
    """End-to-end PR pickup: real resolver, real git fetch, real worktree."""
    # Earlier suite tests rebind backend.project_store (test_mcp_gold_standard's
    # _reset_backend) while vcs_pickup._project_store keeps the import-time
    # instance; pin them to the same store so resolve + worktree agree.
    monkeypatch.setattr(vcs_pickup._service, "project_store", backend.project_store)
    resp = client.post(
        URL,
        headers=auth_headers,
        json=_pr_body(
            head_ref="feature/pr-1",
            project=cloned_project.project_id,  # resolve the managed project directly
        ),
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["accepted"] is True
    assert body["worktree_id"]

    wt = backend.project_store.get_project(body["worktree_id"])
    assert wt is not None and wt.is_worktree
    assert wt.branch == "feature/pr-1"
    assert Path(wt.path).is_dir()
    # The worktree is checked out on the PR branch with its pushed content.
    assert (Path(wt.path) / "fix.txt").read_text() == "fix\n"

    [call] = fake_runtime.start_calls
    assert call["cwd"] == wt.path
    [(_sid, ctx)] = fake_runtime.context_events
    assert ctx["project"] == f"managed:{wt.project_id}"
    assert ctx["branch"] == "feature/pr-1"


# ---------------------------------------------------------------------------
# Config-project identity fallback
# ---------------------------------------------------------------------------


def test_unpromoted_config_project_resolves_by_git_identity(
    client, auth_headers, fake_runtime, monkeypatch, tmp_path
) -> None:
    """``owner/repo`` falls back to config-project remote matching.

    ``_resolve_repo_or_404``'s identity scan covers only managed projects, so
    the first-ever pickup of a config-defined project must match its git
    remotes via ``_config_project_for_repo`` and resolve by config name.
    """
    from mewbo_api import repo_identity

    calls: list[str] = []
    target = _target(project_id=None, name="widget-config", path=str(tmp_path))

    def fake_resolve(key, promote=False):
        calls.append(key)
        if key == "acme/widget":
            return None, ({"message": "Project 'acme/widget' not found"}, 404)
        return target, None

    monkeypatch.setattr(vcs_pickup._service, "resolve_repo", fake_resolve)
    monkeypatch.setattr(
        vcs_pickup,
        "get_config",
        lambda: SimpleNamespace(
            projects={"widget-config": SimpleNamespace(path=str(tmp_path))}
        ),
    )
    monkeypatch.setattr(
        repo_identity.RepoIdentity,
        "aliases_for_path",
        staticmethod(lambda path: ["acme/widget", "widget"]),
    )

    resp = client.post(
        "/api/automation/vcs-pickup", json=_issue_body(), headers=auth_headers
    )
    assert resp.status_code == 200
    assert calls == ["acme/widget", "widget-config"]
    assert fake_runtime.start_calls, "run should start after fallback resolution"


def test_explicit_project_override_skips_identity_fallback(
    client, auth_headers, fake_runtime, monkeypatch
) -> None:
    """A 404 on an explicit ``project`` override is returned, not retried."""

    def fake_resolve(key, promote=False):
        return None, ({"message": f"Project '{key}' not found"}, 404)

    monkeypatch.setattr(vcs_pickup._service, "resolve_repo", fake_resolve)
    resp = client.post(
        "/api/automation/vcs-pickup",
        json=_issue_body(project="explicit-name"),
        headers=auth_headers,
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Reply leg: completion hook + post_comment
# ---------------------------------------------------------------------------


def _pickup_transcript(*, api_url: str | None = "https://git.example.com/api/v1") -> list[dict]:
    vcs = {
        "provider": "gitea",
        "api_url": api_url,
        "repository": "acme/widget",
        "kind": "issue",
        "number": 7,
    }
    return [
        {"type": "context", "payload": {"project": "x", "vcs_pickup": vcs}},
        {"type": "user", "payload": {"text": "do the thing"}},
        {"type": "assistant", "payload": {"text": "All done."}},
    ]


def test_completion_hook_posts_final_answer(fake_runtime, monkeypatch) -> None:
    fake_runtime.session_store.transcript = _pickup_transcript()
    posted: list[tuple] = []
    monkeypatch.setattr(
        vcs_pickup._service, "post_comment", lambda *a: posted.append(a) or True
    )
    vcs_pickup._service.completion_hook("sess-1")
    assert posted == [("https://git.example.com/api/v1", "acme/widget", 7, "All done.")]


def test_completion_hook_reports_run_error(fake_runtime, monkeypatch) -> None:
    fake_runtime.session_store.transcript = _pickup_transcript()
    posted: list[tuple] = []
    monkeypatch.setattr(
        vcs_pickup._service, "post_comment", lambda *a: posted.append(a) or True
    )
    vcs_pickup._service.completion_hook("sess-1", "boom")
    assert posted[0][3] == "Session ended with an error: boom"


@pytest.mark.parametrize("transcript", [
    [{"type": "assistant", "payload": {"text": "hi"}}],  # not a pickup session
    _pickup_transcript(api_url=None),  # workflow predates the reply leg
])
def test_completion_hook_skips_without_reply_target(
    fake_runtime, monkeypatch, transcript
) -> None:
    fake_runtime.session_store.transcript = transcript
    monkeypatch.setattr(
        vcs_pickup._service,
        "post_comment",
        lambda *a: pytest.fail("post_comment must not be called"),
    )
    vcs_pickup._service.completion_hook("sess-1")


class _FakeHttpResponse:
    status = 201

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def vcs_channel_config(monkeypatch):
    """Point ``channels.vcs`` config at a known bot token."""
    monkeypatch.setattr(
        vcs_pickup,
        "get_config",
        lambda: SimpleNamespace(
            channels={"vcs": {"tokens": {"git.example.com": "tok-123"}}}
        ),
    )


def test_post_comment_request_shape(monkeypatch, vcs_channel_config) -> None:
    """One client covers both forges: shared endpoint + token auth scheme."""
    import json as json_mod

    captured: list = []

    def fake_urlopen(req, timeout=0, context=None):
        captured.append(req)
        return _FakeHttpResponse()

    monkeypatch.setattr(vcs_pickup.urllib.request, "urlopen", fake_urlopen)
    ok = vcs_pickup._service.post_comment(
        "https://git.example.com/api/v1/", "acme/widget", 7, "All done."
    )
    assert ok is True
    req = captured[0]
    assert req.full_url == "https://git.example.com/api/v1/repos/acme/widget/issues/7/comments"
    assert req.get_header("Authorization") == "token tok-123"
    assert json_mod.loads(req.data) == {"body": "All done."}


def test_post_comment_truncates_oversized_answer(monkeypatch, vcs_channel_config) -> None:
    import json as json_mod

    captured: list = []
    monkeypatch.setattr(
        vcs_pickup.urllib.request,
        "urlopen",
        lambda req, timeout=0, context=None: captured.append(req) or _FakeHttpResponse(),
    )
    vcs_pickup._service.post_comment(
        "https://git.example.com/api/v1",
        "acme/widget",
        7,
        "x" * (vcs_pickup.VcsPickupService.COMMENT_MAX_CHARS + 1),
    )
    body = json_mod.loads(captured[0].data)["body"]
    assert body.endswith("*[truncated]*")
    assert len(body) < vcs_pickup.VcsPickupService.COMMENT_MAX_CHARS + 100


def test_post_comment_without_token_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(
        vcs_pickup, "get_config", lambda: SimpleNamespace(channels={})
    )
    monkeypatch.setattr(
        vcs_pickup.urllib.request,
        "urlopen",
        lambda *a, **kw: pytest.fail("no HTTP call without a configured token"),
    )
    assert (
        vcs_pickup._service.post_comment(
            "https://git.example.com/api/v1", "acme/widget", 7, "hi"
        )
        is False
    )
