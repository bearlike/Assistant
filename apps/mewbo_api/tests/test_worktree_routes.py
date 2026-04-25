"""Tests for the worktree-management HTTP routes."""

# mypy: ignore-errors

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

if shutil.which("git") is None:  # pragma: no cover
    pytest.skip("git not installed", allow_module_level=True)

from mewbo_api import backend

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth() -> dict[str, str]:
    return {"X-API-KEY": backend.MASTER_API_TOKEN}


def _git(repo: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "t@e.com")
    _git(str(repo), "config", "user.name", "t")
    (repo / "README.md").write_text("hi\n")
    _git(str(repo), "add", "-A")
    _git(str(repo), "commit", "-m", "init")
    _git(str(repo), "branch", "feature/auth")
    return repo


@pytest.fixture
def parent_project(tmp_path: Path):
    """Create a managed project pointing at a real git repo."""
    repo = _make_repo(tmp_path)
    proj = backend.project_store.create_project(
        name="myrepo", description="", path=str(repo)
    )
    yield proj
    # Best-effort cleanup; tests may leave worktrees behind.
    try:
        for wt in backend.project_store.list_worktrees(proj.project_id):
            try:
                backend.project_store.delete_worktree(wt.project_id, force=True)
            except Exception:
                pass
        backend.project_store.delete_project(proj.project_id)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def test_branches_requires_auth(parent_project) -> None:
    """Branches requires auth."""
    client = backend.app.test_client()
    resp = client.get(f"/api/v_projects/{parent_project.project_id}/branches")
    assert resp.status_code == 401


def test_worktrees_requires_auth(parent_project) -> None:
    """Worktrees requires auth."""
    client = backend.app.test_client()
    resp = client.get(f"/api/v_projects/{parent_project.project_id}/worktrees")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Branches
# ---------------------------------------------------------------------------


def test_list_branches_returns_local(parent_project) -> None:
    """List branches returns local."""
    client = backend.app.test_client()
    resp = client.get(
        f"/api/v_projects/{parent_project.project_id}/branches",
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["git_repo"] is True
    assert "main" in body["branches"]
    assert "feature/auth" in body["branches"]


def test_list_branches_unknown_project(tmp_path: Path) -> None:
    """List branches unknown project."""
    client = backend.app.test_client()
    resp = client.get("/api/v_projects/does-not-exist/branches", headers=_auth())
    assert resp.status_code == 404


def test_list_branches_non_git(tmp_path: Path) -> None:
    """List branches non git."""
    plain = tmp_path / "plain"
    plain.mkdir()
    proj = backend.project_store.create_project(
        name="plain", description="", path=str(plain)
    )
    try:
        client = backend.app.test_client()
        resp = client.get(
            f"/api/v_projects/{proj.project_id}/branches", headers=_auth()
        )
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["git_repo"] is False
        assert body["branches"] == []
    finally:
        backend.project_store.delete_project(proj.project_id)


# ---------------------------------------------------------------------------
# Worktree CRUD
# ---------------------------------------------------------------------------


def test_create_and_list_worktree(parent_project) -> None:
    """Create and list worktree."""
    client = backend.app.test_client()
    create = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    assert create.status_code == 201
    body = create.get_json()
    assert body["is_worktree"] is True
    assert body["branch"] == "feature/auth"
    assert body["parent_project_id"] == parent_project.project_id

    listing = client.get(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        headers=_auth(),
    )
    assert listing.status_code == 200
    items = listing.get_json()["worktrees"]
    assert len(items) == 1
    assert items[0]["branch"] == "feature/auth"
    assert items[0]["clean"] is True


def test_create_worktree_missing_branch(parent_project) -> None:
    """Create worktree missing branch."""
    client = backend.app.test_client()
    resp = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={},
        headers=_auth(),
    )
    assert resp.status_code == 400


def test_create_worktree_idempotent(parent_project) -> None:
    """Create worktree idempotent."""
    client = backend.app.test_client()
    client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    # Second call returns the existing worktree (not 409).
    second = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    assert second.status_code == 201


def test_delete_worktree_clean(parent_project) -> None:
    """Delete worktree clean."""
    client = backend.app.test_client()
    create = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    wt_id = create.get_json()["project_id"]
    resp = client.delete(
        f"/api/v_projects/{parent_project.project_id}/worktrees/{wt_id}",
        headers=_auth(),
    )
    assert resp.status_code == 204
    assert backend.project_store.get_project(wt_id) is None


def test_delete_worktree_dirty_returns_409(parent_project) -> None:
    """Delete worktree dirty returns 409."""
    client = backend.app.test_client()
    create = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    body = create.get_json()
    (Path(body["path"]) / "scratch.txt").write_text("dirty\n")

    resp = client.delete(
        f"/api/v_projects/{parent_project.project_id}/worktrees/"
        f"{body['project_id']}",
        headers=_auth(),
    )
    assert resp.status_code == 409
    assert backend.project_store.get_project(body["project_id"]) is not None


def test_delete_worktree_force(parent_project) -> None:
    """Delete worktree force."""
    client = backend.app.test_client()
    create = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    body = create.get_json()
    (Path(body["path"]) / "scratch.txt").write_text("dirty\n")

    resp = client.delete(
        f"/api/v_projects/{parent_project.project_id}/worktrees/"
        f"{body['project_id']}?force=true",
        headers=_auth(),
    )
    assert resp.status_code == 204


def test_delete_worktree_not_found(parent_project) -> None:
    """Delete worktree not found."""
    client = backend.app.test_client()
    resp = client.delete(
        f"/api/v_projects/{parent_project.project_id}/worktrees/wt:bogus:x",
        headers=_auth(),
    )
    assert resp.status_code == 404


def test_cannot_create_worktree_on_a_worktree(parent_project) -> None:
    """A worktree itself cannot be the parent of another worktree."""
    client = backend.app.test_client()
    create = client.post(
        f"/api/v_projects/{parent_project.project_id}/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    wt_id = create.get_json()["project_id"]
    resp = client.post(
        f"/api/v_projects/{wt_id}/worktrees",
        json={"branch": "main"},
        headers=_auth(),
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Auto-cleanup hook
# ---------------------------------------------------------------------------


def test_auto_cleanup_removes_clean_worktree(parent_project) -> None:
    """Session-end hook should drop a clean worktree-backed project."""
    # Create a worktree.
    wt = backend.project_store.create_worktree(
        parent_project.project_id, "feature/auth"
    )
    # Create a session whose context references the worktree.
    session_id = backend.runtime.session_store.create_session()
    backend.runtime.append_context_event(
        session_id, {"project": f"managed:{wt.project_id}"}
    )

    backend._auto_cleanup_worktree_on_session_end(session_id, None)

    assert backend.project_store.get_project(wt.project_id) is None


def test_auto_cleanup_keeps_dirty_worktree(parent_project) -> None:
    """Auto cleanup keeps dirty worktree."""
    wt = backend.project_store.create_worktree(
        parent_project.project_id, "feature/auth"
    )
    (Path(wt.path) / "dirty.txt").write_text("changes\n")

    session_id = backend.runtime.session_store.create_session()
    backend.runtime.append_context_event(
        session_id, {"project": f"managed:{wt.project_id}"}
    )

    backend._auto_cleanup_worktree_on_session_end(session_id, None)

    assert backend.project_store.get_project(wt.project_id) is not None


def test_auto_cleanup_ignores_regular_projects(parent_project) -> None:
    """Hook must not touch non-worktree managed projects or config projects."""
    session_id = backend.runtime.session_store.create_session()
    backend.runtime.append_context_event(
        session_id, {"project": f"managed:{parent_project.project_id}"}
    )

    backend._auto_cleanup_worktree_on_session_end(session_id, None)

    assert backend.project_store.get_project(parent_project.project_id) is not None


def test_auto_cleanup_no_project_in_session(parent_project) -> None:
    """Auto cleanup no project in session."""
    session_id = backend.runtime.session_store.create_session()
    # No context event with a project.
    backend._auto_cleanup_worktree_on_session_end(session_id, None)  # must not raise


# ---------------------------------------------------------------------------
# Worktree context propagation
# ---------------------------------------------------------------------------


def test_populate_worktree_context_sets_repo_and_branch(parent_project) -> None:
    """Populate worktree context sets repo and branch."""
    wt = backend.project_store.create_worktree(
        parent_project.project_id, "feature/auth"
    )
    ctx: dict = {}
    backend._populate_worktree_context(f"managed:{wt.project_id}", ctx)
    assert ctx["branch"] == "feature/auth"
    assert ctx["repo"] == "myrepo"


def test_populate_worktree_context_noop_for_regular_project(parent_project) -> None:
    """Populate worktree context noop for regular project."""
    ctx: dict = {}
    backend._populate_worktree_context(
        f"managed:{parent_project.project_id}", ctx
    )
    assert ctx == {}


def test_populate_worktree_context_noop_for_config_project() -> None:
    """Populate worktree context noop for config project."""
    ctx: dict = {}
    backend._populate_worktree_context("some-config-project", ctx)
    assert ctx == {}


# ---------------------------------------------------------------------------
# Config-project resolution + current_branch
# ---------------------------------------------------------------------------


def _register_config_project(monkeypatch, name: str, path: Path, description: str = "") -> None:
    """Register a configured project so the routes can resolve it by name."""
    cfg = backend.get_config()
    project_cls = type(next(iter(cfg.projects.values()))) if cfg.projects else None
    if project_cls is None:
        from mewbo_core.config import ProjectConfig as project_cls  # type: ignore[no-redef]
    new_entry = project_cls(path=str(path), description=description)
    monkeypatch.setitem(cfg.projects, name, new_entry)


def test_branches_for_config_project(tmp_path: Path, monkeypatch) -> None:
    """A configured project resolves by name and exposes its branches + HEAD."""
    repo = _make_repo(tmp_path)
    _register_config_project(monkeypatch, "MyConfig", repo)
    client = backend.app.test_client()
    resp = client.get("/api/v_projects/MyConfig/branches", headers=_auth())
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["git_repo"] is True
    assert body["current_branch"] == "main"
    assert "main" in body["branches"]
    assert "feature/auth" in body["branches"]


def test_current_branch_in_managed_project_response(parent_project) -> None:
    """Current branch is included in the managed-project branch listing too."""
    client = backend.app.test_client()
    resp = client.get(
        f"/api/v_projects/{parent_project.project_id}/branches",
        headers=_auth(),
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["current_branch"] == "main"


def test_worktrees_for_config_project_lists_user_created(
    tmp_path: Path, monkeypatch
) -> None:
    """User-created git worktrees show up with managed=False for config projects."""
    repo = _make_repo(tmp_path)
    user_wt = tmp_path / "user-wt"
    _git(str(repo), "worktree", "add", str(user_wt), "feature/auth")
    _register_config_project(monkeypatch, "MyConfig2", repo)
    try:
        client = backend.app.test_client()
        resp = client.get("/api/v_projects/MyConfig2/worktrees", headers=_auth())
        assert resp.status_code == 200
        items = resp.get_json()["worktrees"]
        # Parent repo is excluded; only the user-created sibling remains.
        assert len(items) == 1
        entry = items[0]
        assert entry["managed"] is False
        assert entry["branch"] == "feature/auth"
        assert entry["project_id"] is None
    finally:
        try:
            _git(str(repo), "worktree", "remove", "--force", str(user_wt))
        except Exception:
            pass


def test_create_worktree_promotes_config_project(
    tmp_path: Path, monkeypatch
) -> None:
    """Creating a worktree on a config project auto-promotes to a managed parent."""
    repo = _make_repo(tmp_path)
    _git(str(repo), "branch", "feature/extra")
    _register_config_project(monkeypatch, "MyConfig3", repo)
    client = backend.app.test_client()
    create = client.post(
        "/api/v_projects/MyConfig3/worktrees",
        json={"branch": "feature/auth"},
        headers=_auth(),
    )
    assert create.status_code == 201
    body = create.get_json()
    assert body["is_worktree"] is True
    assert body["branch"] == "feature/auth"

    # Promotion is idempotent — the same path doesn't get a second managed entry.
    promoted = backend._find_promoted_for_path(str(repo))
    assert promoted is not None
    parent_id = promoted.project_id

    second = client.post(
        "/api/v_projects/MyConfig3/worktrees",
        json={"branch": "feature/extra"},
        headers=_auth(),
    )
    assert second.status_code == 201
    again = backend._find_promoted_for_path(str(repo))
    assert again is not None and again.project_id == parent_id

    # Cleanup: delete worktrees and the promoted parent.
    for wt in backend.project_store.list_worktrees(parent_id):
        try:
            backend.project_store.delete_worktree(wt.project_id, force=True)
        except Exception:
            pass
    backend.project_store.delete_project(parent_id)


def test_merged_listing_includes_managed_and_user(parent_project, tmp_path: Path) -> None:
    """Merged listing shows both managed worktrees and on-disk user worktrees."""
    # Managed worktree on feature/auth.
    backend.project_store.create_worktree(parent_project.project_id, "feature/auth")
    # User-created worktree on main, off-tree.
    user_wt = tmp_path / "side-by-side-main"
    _git(parent_project.path, "branch", "extra", "main")
    _git(parent_project.path, "worktree", "add", str(user_wt), "extra")
    try:
        client = backend.app.test_client()
        resp = client.get(
            f"/api/v_projects/{parent_project.project_id}/worktrees",
            headers=_auth(),
        )
        assert resp.status_code == 200
        items = resp.get_json()["worktrees"]
        managed = [w for w in items if w["managed"]]
        user = [w for w in items if not w["managed"]]
        assert len(managed) == 1 and managed[0]["branch"] == "feature/auth"
        assert len(user) == 1 and user[0]["branch"] == "extra"
    finally:
        try:
            _git(parent_project.path, "worktree", "remove", "--force", str(user_wt))
        except Exception:
            pass


def test_create_worktree_rejected_on_non_git_config_project(
    tmp_path: Path, monkeypatch
) -> None:
    """Creating a worktree on a config path that is not a git repo errors clearly."""
    plain = tmp_path / "plain"
    plain.mkdir()
    _register_config_project(monkeypatch, "PlainCfg", plain)
    client = backend.app.test_client()
    resp = client.post(
        "/api/v_projects/PlainCfg/worktrees",
        json={"branch": "main"},
        headers=_auth(),
    )
    assert resp.status_code == 400
    assert "not a git repository" in resp.get_json()["message"].lower()
