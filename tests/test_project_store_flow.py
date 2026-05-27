"""Integration/contract tests for project_store.py.

Covers:
- VirtualProject dataclass and worktree_project_id()
- _setup_project_folder() filesystem behaviour
- _render_claude_md() Jinja2 template rendering
- JsonProjectStore CRUD: create, list, get, update, delete
- JsonProjectStore: auto-path vs provided-path
- JsonProjectStore: stale data file graceful handling
- JsonProjectStore: delete renames auto-path folder; skips provided-path
- JsonProjectStore: _persist_worktree() creates worktree record
- ProjectStoreBase.list_worktrees() default impl
- create_project_store() factory selects json vs mongodb (stubbed)

Stubs only real network (MongoDB) — filesystem operations use tmp_path.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from mewbo_core import worktree as worktree_module
from mewbo_core.config import AppConfig, reset_config, set_app_config_path
from mewbo_core.project_store import (
    JsonProjectStore,
    VirtualProject,
    _render_claude_md,
    _setup_project_folder,
    _utc_now,
    create_project_store,
    worktree_project_id,
)


# ---------------------------------------------------------------------------
# Fixture: configure store to use tmp_path for all paths
# ---------------------------------------------------------------------------
@pytest.fixture
def json_store(tmp_path):
    """Return a JsonProjectStore with all paths scoped to tmp_path."""
    cfg_path = tmp_path / "app.json"
    cfg = AppConfig.model_validate(
        {
            "runtime": {
                "projects_home": str(tmp_path / "projects"),
                "config_dir": str(tmp_path),
                "session_dir": str(tmp_path / "sessions"),
                "cache_dir": str(tmp_path / "cache"),
            }
        }
    )
    cfg.write(cfg_path)
    reset_config()
    set_app_config_path(cfg_path)
    return JsonProjectStore()


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------
class TestUtcNow:
    def test_returns_iso_string(self):
        ts = _utc_now()
        assert isinstance(ts, str)
        assert "T" in ts  # ISO format has 'T'


class TestWorktreeProjectId:
    def test_format_is_deterministic(self):
        pid = worktree_project_id("parent-123", "feature/my-branch")
        assert pid.startswith("wt:parent-123:")
        # Same input → same output
        assert pid == worktree_project_id("parent-123", "feature/my-branch")

    def test_different_branches_give_different_ids(self):
        p1 = worktree_project_id("parent", "branch-a")
        p2 = worktree_project_id("parent", "branch-b")
        assert p1 != p2

    def test_different_parents_give_different_ids(self):
        p1 = worktree_project_id("parent-1", "main")
        p2 = worktree_project_id("parent-2", "main")
        assert p1 != p2


# ---------------------------------------------------------------------------
# _render_claude_md()  (line 57–62)
# ---------------------------------------------------------------------------
class TestRenderClaudeMd:
    def test_renders_name_and_description(self):
        result = _render_claude_md("MyProject", "A test project")
        assert "MyProject" in result
        assert "A test project" in result

    def test_returns_nonempty_string(self):
        assert len(_render_claude_md("X", "Y")) > 0


# ---------------------------------------------------------------------------
# _setup_project_folder()  (lines 65–72)
# ---------------------------------------------------------------------------
class TestSetupProjectFolder:
    def test_creates_folder_when_missing(self, tmp_path):
        target = tmp_path / "new_project"
        folder_created = _setup_project_folder(target, "New", "Desc")
        assert target.exists()
        assert folder_created is True

    def test_returns_false_when_folder_already_exists(self, tmp_path):
        target = tmp_path / "existing"
        target.mkdir()
        folder_created = _setup_project_folder(target, "Existing", "Desc")
        assert folder_created is False

    def test_creates_claude_md_in_new_folder(self, tmp_path):
        target = tmp_path / "project_x"
        _setup_project_folder(target, "ProjectX", "Some project")
        assert (target / "CLAUDE.md").exists()
        content = (target / "CLAUDE.md").read_text()
        assert "ProjectX" in content

    def test_does_not_overwrite_existing_claude_md(self, tmp_path):
        target = tmp_path / "my_project"
        target.mkdir()
        custom = target / "CLAUDE.md"
        custom.write_text("CUSTOM CONTENT")
        _setup_project_folder(target, "my_project", "desc")
        assert custom.read_text() == "CUSTOM CONTENT"

    def test_creates_nested_dirs(self, tmp_path):
        target = tmp_path / "a" / "b" / "c"
        _setup_project_folder(target, "deep", "nested")
        assert target.exists()


# ---------------------------------------------------------------------------
# JsonProjectStore.create_project()  (lines 229–255)
# ---------------------------------------------------------------------------
class TestJsonProjectStoreCreate:
    def test_create_returns_virtual_project(self, json_store):
        proj = json_store.create_project("Test", "A test project")
        assert isinstance(proj, VirtualProject)
        assert proj.name == "Test"
        assert proj.description == "A test project"
        assert proj.project_id
        assert proj.created_at
        assert proj.updated_at

    def test_create_with_auto_path_creates_folder_in_projects_home(self, json_store, tmp_path):
        proj = json_store.create_project("AutoPath", "Auto")
        assert Path(proj.path).exists()
        assert proj.path_source == "auto"
        assert str(tmp_path / "projects") in proj.path

    def test_create_with_provided_path(self, json_store, tmp_path):
        custom = tmp_path / "custom_dir"
        proj = json_store.create_project("Custom", "Desc", path=str(custom))
        assert proj.path_source == "provided"
        assert Path(proj.path).exists()

    def test_create_persists_to_data_file(self, json_store):
        proj = json_store.create_project("Persisted", "desc")
        records = json.loads(json_store._data_file.read_text())
        ids = [r["project_id"] for r in records]
        assert proj.project_id in ids

    def test_creates_claude_md_in_project_folder(self, json_store):
        proj = json_store.create_project("WithMd", "Has CLAUDE.md")
        claude_md = Path(proj.path) / "CLAUDE.md"
        assert claude_md.exists()


# ---------------------------------------------------------------------------
# JsonProjectStore.list_projects()  (lines 285–287)
# ---------------------------------------------------------------------------
class TestJsonProjectStoreList:
    def test_empty_store_returns_empty_list(self, json_store):
        assert json_store.list_projects() == []

    def test_list_returns_all_created_projects(self, json_store):
        json_store.create_project("P1", "d1")
        json_store.create_project("P2", "d2")
        projects = json_store.list_projects()
        assert len(projects) == 2
        names = {p.name for p in projects}
        assert names == {"P1", "P2"}

    def test_list_returns_virtual_project_instances(self, json_store):
        json_store.create_project("P", "d")
        for p in json_store.list_projects():
            assert isinstance(p, VirtualProject)


# ---------------------------------------------------------------------------
# JsonProjectStore.get_project()  (lines 289–294)
# ---------------------------------------------------------------------------
class TestJsonProjectStoreGet:
    def test_get_existing_project(self, json_store):
        proj = json_store.create_project("GetMe", "desc")
        fetched = json_store.get_project(proj.project_id)
        assert fetched is not None
        assert fetched.project_id == proj.project_id
        assert fetched.name == "GetMe"

    def test_get_nonexistent_returns_none(self, json_store):
        assert json_store.get_project("nonexistent-id") is None

    def test_get_handles_stale_data_file(self, json_store):
        """Corrupt data file → empty list → get returns None."""
        json_store._data_file.parent.mkdir(parents=True, exist_ok=True)
        json_store._data_file.write_text("INVALID JSON")
        assert json_store.get_project("any-id") is None


# ---------------------------------------------------------------------------
# JsonProjectStore.update_project()  (lines 296–310)
# ---------------------------------------------------------------------------
class TestJsonProjectStoreUpdate:
    def test_update_name(self, json_store):
        proj = json_store.create_project("Old", "desc")
        updated = json_store.update_project(proj.project_id, name="New")
        assert updated.name == "New"
        assert updated.description == "desc"

    def test_update_description(self, json_store):
        proj = json_store.create_project("Name", "OldDesc")
        updated = json_store.update_project(proj.project_id, description="NewDesc")
        assert updated.name == "Name"
        assert updated.description == "NewDesc"

    def test_update_both(self, json_store):
        proj = json_store.create_project("N", "D")
        updated = json_store.update_project(proj.project_id, name="N2", description="D2")
        assert updated.name == "N2"
        assert updated.description == "D2"

    def test_update_updates_updated_at(self, json_store):
        proj = json_store.create_project("N", "D")
        orig_ts = proj.updated_at
        time.sleep(0.01)  # ensure timestamp changes
        updated = json_store.update_project(proj.project_id, name="New")
        assert updated.updated_at >= orig_ts

    def test_update_nonexistent_raises_key_error(self, json_store):
        with pytest.raises(KeyError, match="not found"):
            json_store.update_project("no-such-id", name="X")

    def test_update_persists_to_file(self, json_store):
        proj = json_store.create_project("N", "D")
        json_store.update_project(proj.project_id, name="Updated")
        records = json.loads(json_store._data_file.read_text())
        match = next(r for r in records if r["project_id"] == proj.project_id)
        assert match["name"] == "Updated"


# ---------------------------------------------------------------------------
# JsonProjectStore.delete_project()  (lines 312–325)
# ---------------------------------------------------------------------------
class TestJsonProjectStoreDelete:
    def test_delete_removes_from_list(self, json_store):
        proj = json_store.create_project("Del", "d")
        json_store.delete_project(proj.project_id)
        assert json_store.get_project(proj.project_id) is None
        assert json_store.list_projects() == []

    def test_delete_auto_path_renames_folder(self, json_store):
        proj = json_store.create_project("AutoDel", "d")
        original_path = Path(proj.path)
        assert original_path.exists()
        json_store.delete_project(proj.project_id)
        # Folder should be renamed to .deleted
        assert not original_path.exists()
        assert Path(str(original_path) + ".deleted").exists()

    def test_delete_provided_path_does_not_rename_folder(self, json_store, tmp_path):
        custom = tmp_path / "custom_kept"
        proj = json_store.create_project("ProvidedDel", "d", path=str(custom))
        assert Path(proj.path).exists()
        json_store.delete_project(proj.project_id)
        # Provided-path folder stays untouched
        assert Path(proj.path).exists()

    def test_delete_nonexistent_does_not_raise(self, json_store):
        # Deleting a non-existent project silently succeeds (no remaining records)
        json_store.delete_project("ghost-id")

    def test_delete_persists_removal_to_file(self, json_store):
        proj = json_store.create_project("ToDelete", "d")
        json_store.delete_project(proj.project_id)
        records = json.loads(json_store._data_file.read_text())
        assert all(r["project_id"] != proj.project_id for r in records)


# ---------------------------------------------------------------------------
# JsonProjectStore._persist_worktree()  (lines 257–283)
# ---------------------------------------------------------------------------
class TestJsonProjectStorePersistWorktree:
    def test_persist_worktree_creates_record(self, json_store, tmp_path):
        parent = json_store.create_project("Parent", "p")
        wt_path = tmp_path / "wt"
        wt_path.mkdir()
        wt = json_store._persist_worktree(
            project_id=worktree_project_id(parent.project_id, "feature/x"),
            parent_project_id=parent.project_id,
            branch="feature/x",
            path=str(wt_path),
        )
        assert wt.is_worktree is True
        assert wt.branch == "feature/x"
        assert wt.parent_project_id == parent.project_id

    def test_persist_worktree_retrievable(self, json_store, tmp_path):
        parent = json_store.create_project("Parent", "p")
        wt_path = tmp_path / "wt2"
        wt_path.mkdir()
        wt_id = worktree_project_id(parent.project_id, "dev")
        json_store._persist_worktree(
            project_id=wt_id,
            parent_project_id=parent.project_id,
            branch="dev",
            path=str(wt_path),
        )
        fetched = json_store.get_project(wt_id)
        assert fetched is not None
        assert fetched.is_worktree is True


# ---------------------------------------------------------------------------
# ProjectStoreBase.list_worktrees()  (lines 106–112)
# ---------------------------------------------------------------------------
class TestListWorktrees:
    def test_list_worktrees_returns_only_worktrees_for_parent(self, json_store, tmp_path):
        parent1 = json_store.create_project("P1", "p1")
        parent2 = json_store.create_project("P2", "p2")
        # Persist a worktree for parent1
        wt_path = tmp_path / "wt_p1"
        wt_path.mkdir()
        json_store._persist_worktree(
            project_id=worktree_project_id(parent1.project_id, "b1"),
            parent_project_id=parent1.project_id,
            branch="b1",
            path=str(wt_path),
        )
        # No worktrees for parent2
        wts1 = json_store.list_worktrees(parent1.project_id)
        wts2 = json_store.list_worktrees(parent2.project_id)
        assert len(wts1) == 1
        assert wts1[0].branch == "b1"
        assert wts2 == []

    def test_list_worktrees_excludes_regular_projects(self, json_store):
        parent = json_store.create_project("P", "d")
        child = json_store.create_project("Child", "d")  # regular, not worktree
        wts = json_store.list_worktrees(parent.project_id)
        assert child.project_id not in [w.project_id for w in wts]


# ---------------------------------------------------------------------------
# Missing file / stale data handling
# ---------------------------------------------------------------------------
class TestJsonProjectStoreMissingFile:
    def test_list_on_missing_data_file_returns_empty(self, json_store):
        # Data file not created yet
        if json_store._data_file.exists():
            json_store._data_file.unlink()
        assert json_store.list_projects() == []

    def test_load_invalid_json_returns_empty(self, json_store):
        json_store._data_file.parent.mkdir(parents=True, exist_ok=True)
        json_store._data_file.write_text("not valid json")
        # _load() catches exception and returns []
        with json_store._lock:
            result = json_store._load()
        assert result == []


# ---------------------------------------------------------------------------
# Thread safety — concurrent creates don't corrupt the data file
# ---------------------------------------------------------------------------
class TestJsonProjectStoreThreadSafety:
    def test_concurrent_creates_all_persisted(self, json_store):
        errors = []

        def _create(n):
            try:
                json_store.create_project(f"Proj{n}", "desc")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_create, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        projects = json_store.list_projects()
        assert len(projects) == 10


# ---------------------------------------------------------------------------
# create_project_store() factory  (lines 443–449)
# ---------------------------------------------------------------------------
class TestCreateProjectStoreFactory:
    def test_returns_json_store_by_default(self, tmp_path):
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {
                "storage": {"driver": "json"},
                "runtime": {
                    "config_dir": str(tmp_path),
                    "projects_home": str(tmp_path / "projects"),
                },
            }
        ).write(cfg_path)
        reset_config()
        set_app_config_path(cfg_path)
        store = create_project_store()
        assert isinstance(store, JsonProjectStore)

    def test_returns_mongo_store_when_driver_is_mongodb(self, tmp_path, monkeypatch):
        """create_project_store() instantiates MongoProjectStore for mongodb driver."""
        cfg_path = tmp_path / "app.json"
        AppConfig.model_validate(
            {
                "storage": {
                    "driver": "mongodb",
                    "mongodb": {"uri": "mongodb://localhost:27017", "database": "test"},
                },
                "runtime": {
                    "config_dir": str(tmp_path),
                    "projects_home": str(tmp_path / "projects"),
                },
            }
        ).write(cfg_path)
        reset_config()
        set_app_config_path(cfg_path)

        # Stub pymongo so we don't need a live MongoDB server
        fake_col = MagicMock()
        fake_db = MagicMock()
        fake_db.__getitem__ = MagicMock(return_value=fake_col)
        fake_client = MagicMock()
        fake_client.__getitem__ = MagicMock(return_value=fake_db)

        with patch(
            "mewbo_core.project_store.MongoProjectStore.__init__", return_value=None
        ) as m_init:
            create_project_store()
            assert m_init.called


# ---------------------------------------------------------------------------
# VirtualProject dataclass fields
# ---------------------------------------------------------------------------
class TestVirtualProjectDataclass:
    def test_default_path_source_is_auto(self):
        from mewbo_core.project_store import _utc_now

        vp = VirtualProject(
            project_id="id",
            name="n",
            description="d",
            created_at=_utc_now(),
            updated_at=_utc_now(),
            path="/tmp/p",
        )
        assert vp.path_source == "auto"
        assert vp.folder_created is True
        assert vp.is_worktree is False
        assert vp.parent_project_id is None
        assert vp.branch is None


# ---------------------------------------------------------------------------
# create_worktree() and delete_worktree() via stubbed WorktreeManager
# ---------------------------------------------------------------------------
class TestCreateWorktree:
    def test_create_worktree_raises_when_parent_not_found(self, json_store):
        with pytest.raises(KeyError, match="not found"):
            json_store.create_worktree("nonexistent-parent", "feature/x")

    def test_create_worktree_returns_existing_when_path_exists(
        self, json_store, tmp_path, monkeypatch
    ):
        """Idempotent: returns existing record when worktree path still exists."""
        parent = json_store.create_project("P", "d")
        wt_path = tmp_path / "wt_existing"
        wt_path.mkdir()
        # Persist worktree record manually
        wt_id = worktree_project_id(parent.project_id, "feature/y")
        json_store._persist_worktree(
            project_id=wt_id,
            parent_project_id=parent.project_id,
            branch="feature/y",
            path=str(wt_path),
        )
        # create_worktree should return the existing record without calling WorktreeManager
        create_called = []
        monkeypatch.setattr(
            worktree_module.WorktreeManager,
            "create",
            lambda *a, **k: create_called.append(True) or str(wt_path),
        )
        result = json_store.create_worktree(parent.project_id, "feature/y")
        assert result.project_id == wt_id
        assert create_called == []  # WorktreeManager.create not called

    def test_create_worktree_drops_stale_record_and_recreates(
        self, json_store, tmp_path, monkeypatch
    ):
        """Stale record (path gone) is dropped and recreated via WorktreeManager."""
        parent = json_store.create_project("P", "d")
        stale_path = tmp_path / "stale_wt"
        # Don't create the stale path — it's intentionally missing
        wt_id = worktree_project_id(parent.project_id, "stale-branch")
        json_store._persist_worktree(
            project_id=wt_id,
            parent_project_id=parent.project_id,
            branch="stale-branch",
            path=str(stale_path),
        )
        new_path = tmp_path / "new_wt"
        new_path.mkdir()
        monkeypatch.setattr(
            worktree_module.WorktreeManager, "create", staticmethod(lambda *a, **k: str(new_path))
        )
        result = json_store.create_worktree(parent.project_id, "stale-branch")
        assert result.path == str(new_path)
        assert result.is_worktree is True

    def test_create_worktree_calls_worktree_manager_create(self, json_store, tmp_path, monkeypatch):
        """create_worktree invokes WorktreeManager.create when no existing record."""
        parent = json_store.create_project("P", "d")
        new_path = tmp_path / "fresh_wt"
        new_path.mkdir()

        created_args = []
        monkeypatch.setattr(
            worktree_module.WorktreeManager,
            "create",
            staticmethod(
                lambda path, branch, base=None: (
                    created_args.append((path, branch, base)) or str(new_path)
                )
            ),
        )
        result = json_store.create_worktree(parent.project_id, "new-feature", base=None)
        assert len(created_args) == 1
        assert created_args[0][1] == "new-feature"
        assert result.is_worktree is True


class TestDeleteWorktree:
    def test_delete_worktree_raises_for_nonexistent_project(self, json_store):
        with pytest.raises(KeyError, match="not found"):
            json_store.delete_worktree("nonexistent-wt-id")

    def test_delete_worktree_raises_for_non_worktree_project(self, json_store, monkeypatch):
        """Raises KeyError when the project_id refers to a regular (non-worktree) project."""
        regular = json_store.create_project("Regular", "d")
        with pytest.raises(KeyError, match="not found"):
            json_store.delete_worktree(regular.project_id)

    def test_delete_worktree_removes_record_and_calls_manager(
        self, json_store, tmp_path, monkeypatch
    ):
        """delete_worktree calls WorktreeManager.remove and deletes the record."""
        parent = json_store.create_project("P", "d")
        wt_path = tmp_path / "wt_del"
        wt_path.mkdir()
        wt = json_store._persist_worktree(
            project_id=worktree_project_id(parent.project_id, "del-branch"),
            parent_project_id=parent.project_id,
            branch="del-branch",
            path=str(wt_path),
        )

        remove_called = []
        monkeypatch.setattr(
            worktree_module.WorktreeManager,
            "remove",
            staticmethod(lambda path, force=False: remove_called.append(path)),
        )
        # del-branch is not a mewbo branch, so delete_branch should NOT be called
        delete_branch_called = []
        monkeypatch.setattr(
            worktree_module.WorktreeManager,
            "delete_branch",
            staticmethod(lambda *a: delete_branch_called.append(a)),
        )

        json_store.delete_worktree(wt.project_id)
        assert len(remove_called) == 1
        assert json_store.get_project(wt.project_id) is None
        assert delete_branch_called == []  # non-mewbo branch not deleted

    def test_delete_worktree_deletes_mewbo_branch(self, json_store, tmp_path, monkeypatch):
        """For mewbo/ branches, WorktreeManager.delete_branch is called."""
        parent = json_store.create_project("P", "d")
        wt_path = tmp_path / "wt_mewbo"
        wt_path.mkdir()
        wt = json_store._persist_worktree(
            project_id=worktree_project_id(parent.project_id, "mewbo/test-session"),
            parent_project_id=parent.project_id,
            branch="mewbo/test-session",
            path=str(wt_path),
        )

        monkeypatch.setattr(
            worktree_module.WorktreeManager, "remove", staticmethod(lambda path, force=False: None)
        )
        delete_branch_called = []
        monkeypatch.setattr(
            worktree_module.WorktreeManager,
            "delete_branch",
            staticmethod(lambda repo_path, branch: delete_branch_called.append(branch)),
        )

        json_store.delete_worktree(wt.project_id)
        assert "mewbo/test-session" in delete_branch_called


class TestVirtualProjectWorktreeDataclass:
    def test_default_path_source_is_auto(self):
        from mewbo_core.project_store import _utc_now

        vp = VirtualProject(
            project_id="wt:parent:branch",
            name="branch",
            description="wt desc",
            created_at=_utc_now(),
            updated_at=_utc_now(),
            path="/tmp/wt",
            is_worktree=True,
            parent_project_id="parent",
            branch="feature/x",
        )
        assert vp.is_worktree is True
        assert vp.parent_project_id == "parent"
        assert vp.branch == "feature/x"
