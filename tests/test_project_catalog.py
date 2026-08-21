"""Tests for project_catalog.py — ProjectCatalog listing + resolution, ProjectEntry.

Covers:
- entries(): ordering (configured, then managed/worktree, then repository), empty-path
  configured skip, managed/worktree kind + branch/parent_key, availability, repository
  checkout resolution, per-section degrade-on-raise.
- resolve(): success across all three runnable kinds, every ProjectResolutionError code
  (auto_sentinel/not_found/no_checkout/unavailable/empty), key stripping, the
  not-found/empty-catalog hint contract.
- ProjectEntry / is_auto_project model behaviour.

Uses real directories via ``tmp_path`` and small fake ``ProjectStoreBase``/
``RepositoryStoreBase`` implementations rather than mocking the catalog itself.
"""

from __future__ import annotations

import pytest
from mewbo_core.config import ProjectConfig
from mewbo_core.workspaces.project_catalog import (
    AUTO_PROJECT,
    MANAGED_PREFIX,
    ProjectCatalog,
    ProjectEntry,
    ProjectResolutionError,
    is_auto_project,
)
from mewbo_core.workspaces.project_store import ProjectStoreBase, VirtualProject, _utc_now
from mewbo_core.workspaces.repositories import Repository
from mewbo_core.workspaces.repository_store import RepositoryStoreBase
from pydantic import ValidationError

# ---------------------------------------------------------------------------
# Fake stores — canned data on the one method the catalog calls, everything
# else on the ABC raises so an accidental extra call fails loudly.
# ---------------------------------------------------------------------------


class _FakeProjectStore(ProjectStoreBase):
    def __init__(
        self, projects: list[VirtualProject] | None = None, *, raise_on_list: bool = False
    ) -> None:
        self._projects = list(projects or [])
        self._raise_on_list = raise_on_list

    def list_projects(self) -> list[VirtualProject]:
        if self._raise_on_list:
            raise RuntimeError("project store unavailable")
        return list(self._projects)

    def create_project(self, name, description, path=None):  # noqa: D102
        raise NotImplementedError

    def get_project(self, project_id):  # noqa: D102
        raise NotImplementedError

    def update_project(self, project_id, name=None, description=None):  # noqa: D102
        raise NotImplementedError

    def delete_project(self, project_id):  # noqa: D102
        raise NotImplementedError

    def _persist_worktree(self, *, project_id, parent_project_id, branch, path):  # noqa: D102
        raise NotImplementedError


class _FakeRepositoryStore(RepositoryStoreBase):
    def __init__(
        self, repositories: list[Repository] | None = None, *, raise_on_list: bool = False
    ) -> None:
        self._repositories = list(repositories or [])
        self._raise_on_list = raise_on_list

    def _read_all(self) -> list[Repository]:
        if self._raise_on_list:
            raise RuntimeError("repository store unavailable")
        return list(self._repositories)

    def _read(self, slug):  # noqa: D102
        raise NotImplementedError

    def _write(self, repository):  # noqa: D102
        raise NotImplementedError

    def _remove(self, slug):  # noqa: D102
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _virtual_project(
    project_id: str,
    path: str,
    *,
    name: str | None = None,
    is_worktree: bool = False,
    parent_project_id: str | None = None,
    branch: str | None = None,
) -> VirtualProject:
    now = _utc_now()
    return VirtualProject(
        project_id=project_id,
        name=name or project_id,
        description="",
        created_at=now,
        updated_at=now,
        path=path,
        is_worktree=is_worktree,
        parent_project_id=parent_project_id,
        branch=branch,
    )


def _repository(slug: str = "github.com/acme/widgets", **overrides) -> Repository:
    return Repository(slug=slug, **overrides)


# ---------------------------------------------------------------------------
# Listing — entries()
# ---------------------------------------------------------------------------


def test_configured_projects_listed_in_config_order(tmp_path):
    """Iteration follows the mapping's own order, not a sort."""
    beta_dir = tmp_path / "beta"
    alpha_dir = tmp_path / "alpha"
    beta_dir.mkdir()
    alpha_dir.mkdir()
    configured = {
        "beta": ProjectConfig(path=str(beta_dir), description="second-ish"),
        "alpha": ProjectConfig(path=str(alpha_dir), description="first-ish"),
    }
    catalog = ProjectCatalog(configured=configured)

    keys = [e.key for e in catalog.entries()]

    assert keys == ["beta", "alpha"]


def test_configured_project_with_empty_path_is_skipped():
    configured = {
        "ghost": ProjectConfig(path="", description="never registered a directory"),
    }
    catalog = ProjectCatalog(configured=configured)

    assert catalog.entries() == ()


def test_entries_order_is_configured_then_managed_then_repository(tmp_path):
    configured_dir = tmp_path / "configured"
    managed_dir = tmp_path / "managed"
    configured_dir.mkdir()
    managed_dir.mkdir()
    configured = {"cfg": ProjectConfig(path=str(configured_dir), description="")}
    project_store = _FakeProjectStore([_virtual_project("proj-1", str(managed_dir))])
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(
        configured=configured, project_store=project_store, repository_store=repository_store
    )

    kinds = [e.kind for e in catalog.entries()]

    assert kinds == ["configured", "managed", "repository"]


def test_managed_project_surfaces_with_managed_prefix_and_kind(tmp_path):
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    vp = _virtual_project("proj-1", str(managed_dir), name="Proj One")
    project_store = _FakeProjectStore([vp])
    catalog = ProjectCatalog(configured={}, project_store=project_store)

    (entry,) = catalog.entries()

    assert entry.key == f"{MANAGED_PREFIX}proj-1"
    assert entry.kind == "managed"
    assert entry.name == "Proj One"
    assert entry.path == str(managed_dir)


def test_worktree_surfaces_with_worktree_kind_branch_and_parent_key(tmp_path):
    wt_dir = tmp_path / "wt"
    wt_dir.mkdir()
    project_store = _FakeProjectStore(
        [
            _virtual_project(
                "wt-1",
                str(wt_dir),
                is_worktree=True,
                parent_project_id="parent-1",
                branch="feature/x",
            )
        ]
    )
    catalog = ProjectCatalog(configured={}, project_store=project_store)

    (entry,) = catalog.entries()

    assert entry.kind == "worktree"
    assert entry.branch == "feature/x"
    assert entry.parent_key == f"{MANAGED_PREFIX}parent-1"


def test_availability_reflects_whether_directory_exists(tmp_path):
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    missing_dir = tmp_path / "does-not-exist"
    configured = {
        "present": ProjectConfig(path=str(real_dir), description=""),
        "absent": ProjectConfig(path=str(missing_dir), description=""),
    }
    catalog = ProjectCatalog(configured=configured)

    entries = {e.key: e for e in catalog.entries()}

    assert entries["present"].available is True
    assert entries["absent"].available is False


def test_repository_with_no_checkout_locator_has_no_path(tmp_path):
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(configured={}, repository_store=repository_store)

    (entry,) = catalog.entries()

    assert entry.kind == "repository"
    assert entry.path is None
    assert entry.available is False
    assert entry.repo == "github.com/acme/widgets"


def test_repository_with_checkout_locator_resolves_to_checkout_path(tmp_path):
    checkout_dir = tmp_path / "checkout"
    checkout_dir.mkdir()
    repository_store = _FakeRepositoryStore([_repository()])
    def _locate(slug: str) -> str | None:
        return str(checkout_dir) if slug == "github.com/acme/widgets" else None

    catalog = ProjectCatalog(
        configured={}, repository_store=repository_store, checkout_locator=_locate
    )

    (entry,) = catalog.entries()

    assert entry.path == str(checkout_dir)
    assert entry.available is True


def test_repository_checkout_already_listed_as_managed_is_not_duplicated(tmp_path):
    shared_dir = tmp_path / "shared-checkout"
    shared_dir.mkdir()
    project_store = _FakeProjectStore([_virtual_project("proj-1", str(shared_dir))])
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(
        configured={},
        project_store=project_store,
        repository_store=repository_store,
        checkout_locator=lambda slug: str(shared_dir),
    )

    repository_kind_entries = [e for e in catalog.entries() if e.kind == "repository"]

    assert repository_kind_entries == []


def test_repository_dedup_stamps_repo_slug_onto_the_surviving_managed_entry(tmp_path):
    """The skipped duplicate isn't just dropped — its identity rides the survivor."""
    shared_dir = tmp_path / "shared-checkout"
    shared_dir.mkdir()
    project_store = _FakeProjectStore([_virtual_project("proj-1", str(shared_dir))])
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(
        configured={},
        project_store=project_store,
        repository_store=repository_store,
        checkout_locator=lambda slug: str(shared_dir),
    )

    (entry,) = catalog.entries()

    assert entry.kind == "managed"
    assert entry.repo == "github.com/acme/widgets"


def test_repository_dedup_is_symlink_tolerant(tmp_path):
    """A checkout reached through a symlink still dedups against its real target."""
    real_dir = tmp_path / "real-checkout"
    real_dir.mkdir()
    symlink_path = tmp_path / "checkout-via-symlink"
    symlink_path.symlink_to(real_dir)
    project_store = _FakeProjectStore([_virtual_project("proj-1", str(real_dir))])
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(
        configured={},
        project_store=project_store,
        repository_store=repository_store,
        checkout_locator=lambda slug: str(symlink_path),
    )

    entries = catalog.entries()

    assert [e.kind for e in entries] == ["managed"]
    assert entries[0].repo == "github.com/acme/widgets"


def test_raising_project_store_degrades_only_the_managed_section(tmp_path):
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()
    configured = {"cfg": ProjectConfig(path=str(configured_dir), description="")}
    project_store = _FakeProjectStore(raise_on_list=True)
    catalog = ProjectCatalog(configured=configured, project_store=project_store)

    entries = catalog.entries()

    assert [e.key for e in entries] == ["cfg"]


def test_raising_repository_store_degrades_only_the_repository_section(tmp_path):
    configured_dir = tmp_path / "configured"
    configured_dir.mkdir()
    configured = {"cfg": ProjectConfig(path=str(configured_dir), description="")}
    repository_store = _FakeRepositoryStore(raise_on_list=True)
    catalog = ProjectCatalog(configured=configured, repository_store=repository_store)

    entries = catalog.entries()

    assert [e.key for e in entries] == ["cfg"]


# ---------------------------------------------------------------------------
# Ownership — owns_path()
# ---------------------------------------------------------------------------


def test_owns_path_returns_true_for_configured_project_path(tmp_path):
    project_dir = tmp_path / "configured"
    project_dir.mkdir()
    catalog = ProjectCatalog(
        configured={"cfg": ProjectConfig(path=str(project_dir), description="")}
    )

    assert catalog.owns_path(str(project_dir)) is True


def test_owns_path_returns_true_for_managed_project_path(tmp_path):
    project_dir = tmp_path / "managed"
    project_dir.mkdir()
    project_store = _FakeProjectStore([_virtual_project("proj-1", str(project_dir))])
    catalog = ProjectCatalog(configured={}, project_store=project_store)

    assert catalog.owns_path(str(project_dir)) is True


def test_owns_path_returns_false_for_unrelated_directory(tmp_path):
    project_dir = tmp_path / "configured"
    unrelated_dir = tmp_path / "unrelated"
    project_dir.mkdir()
    unrelated_dir.mkdir()
    catalog = ProjectCatalog(
        configured={"cfg": ProjectConfig(path=str(project_dir), description="")}
    )

    assert catalog.owns_path(str(unrelated_dir)) is False


@pytest.mark.parametrize("path", [None, ""])
def test_owns_path_returns_false_for_empty_path(path):
    catalog = ProjectCatalog(configured={})

    assert catalog.owns_path(path) is False


def test_owns_path_returns_true_for_configured_project_symlink(tmp_path):
    project_dir = tmp_path / "configured"
    project_dir.mkdir()
    project_symlink = tmp_path / "configured-via-symlink"
    project_symlink.symlink_to(project_dir)
    catalog = ProjectCatalog(
        configured={"cfg": ProjectConfig(path=str(project_dir), description="")}
    )

    assert catalog.owns_path(str(project_symlink)) is True


# ---------------------------------------------------------------------------
# Resolution — resolve()
# ---------------------------------------------------------------------------


def _single_entry_catalog(tmp_path, kind: str) -> tuple[ProjectCatalog, str, str]:
    """Build a catalog with one runnable entry of *kind*; return (catalog, key, path)."""
    project_dir = tmp_path / kind
    project_dir.mkdir()
    if kind == "configured":
        cfg = ProjectConfig(path=str(project_dir), description="")
        catalog = ProjectCatalog(configured={"myproj": cfg})
        return catalog, "myproj", cfg.path
    if kind == "managed":
        project_store = _FakeProjectStore([_virtual_project("proj-1", str(project_dir))])
        catalog = ProjectCatalog(configured={}, project_store=project_store)
        return catalog, f"{MANAGED_PREFIX}proj-1", str(project_dir)
    if kind == "worktree":
        project_store = _FakeProjectStore(
            [
                _virtual_project(
                    "wt-1", str(project_dir), is_worktree=True, parent_project_id="parent-1"
                )
            ]
        )
        catalog = ProjectCatalog(configured={}, project_store=project_store)
        return catalog, f"{MANAGED_PREFIX}wt-1", str(project_dir)
    raise AssertionError(f"unhandled kind: {kind}")


@pytest.mark.parametrize("kind", ["configured", "managed", "worktree"])
def test_resolve_returns_absolute_path_for_every_runnable_kind(tmp_path, kind):
    catalog, key, expected_path = _single_entry_catalog(tmp_path, kind)

    assert catalog.resolve(key) == expected_path


def test_resolve_auto_sentinel_raises_auto_sentinel_code():
    catalog = ProjectCatalog(configured={})

    with pytest.raises(ProjectResolutionError) as excinfo:
        catalog.resolve(AUTO_PROJECT)

    assert excinfo.value.code == "auto_sentinel"


def test_resolve_unknown_key_names_available_keys_in_message(tmp_path):
    known_dir = tmp_path / "known"
    known_dir.mkdir()
    configured = {"alpha": ProjectConfig(path=str(known_dir), description="")}
    catalog = ProjectCatalog(configured=configured)

    with pytest.raises(ProjectResolutionError) as excinfo:
        catalog.resolve("does-not-exist")

    assert excinfo.value.code == "not_found"
    assert "'alpha'" in str(excinfo.value)


def test_resolve_repository_with_no_checkout_raises_no_checkout_code():
    repository_store = _FakeRepositoryStore([_repository()])
    catalog = ProjectCatalog(configured={}, repository_store=repository_store)

    with pytest.raises(ProjectResolutionError) as excinfo:
        catalog.resolve("github.com/acme/widgets")

    assert excinfo.value.code == "no_checkout"


def test_resolve_missing_directory_raises_unavailable_with_path_in_message(tmp_path):
    missing_dir = tmp_path / "gone"
    configured = {"vanished": ProjectConfig(path=str(missing_dir), description="")}
    catalog = ProjectCatalog(configured=configured)

    with pytest.raises(ProjectResolutionError) as excinfo:
        catalog.resolve("vanished")

    assert excinfo.value.code == "unavailable"
    # ProjectConfig resolves the path, so compare against the entry's own value
    # rather than the raw tmp_path string.
    resolved_path = catalog.entries()[0].path
    assert resolved_path in str(excinfo.value)


@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_resolve_empty_or_whitespace_key_raises_empty_code(key):
    catalog = ProjectCatalog(configured={})

    with pytest.raises(ProjectResolutionError) as excinfo:
        catalog.resolve(key)

    assert excinfo.value.code == "empty"


def test_resolve_strips_surrounding_whitespace_before_matching(tmp_path):
    catalog, key, expected_path = _single_entry_catalog(tmp_path, "configured")

    assert catalog.resolve(f"  {key}\t") == expected_path


def test_available_hint_says_so_when_nothing_is_runnable():
    catalog = ProjectCatalog(configured={})

    assert catalog._available_hint() == "No projects are currently available."


def test_available_hint_truncates_past_twelve_and_mentions_remainder(tmp_path):
    configured = {}
    for i in range(15):
        project_dir = tmp_path / f"p{i}"
        project_dir.mkdir()
        configured[f"p{i}"] = ProjectConfig(path=str(project_dir), description="")
    catalog = ProjectCatalog(configured=configured)

    hint = catalog._available_hint()

    assert "'p11'" in hint
    assert "'p12'" not in hint
    assert "(and 3 more)" in hint


# ---------------------------------------------------------------------------
# Model — ProjectEntry / is_auto_project
# ---------------------------------------------------------------------------


def test_project_entry_rejects_unknown_field():
    with pytest.raises(ValidationError):
        ProjectEntry(key="k", name="n", kind="configured", not_a_real_field="nope")


@pytest.mark.parametrize(
    ("path", "available", "expected"),
    [
        ("/some/path", True, True),
        ("/some/path", False, False),
        (None, True, False),
        (None, False, False),
    ],
)
def test_project_entry_runnable_requires_path_and_availability(path, available, expected):
    entry = ProjectEntry(key="k", name="n", kind="configured", path=path, available=available)

    assert entry.runnable is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (AUTO_PROJECT, True),
        (f"  {AUTO_PROJECT}  ", True),
        ("Auto", False),
        ("AUTO", False),
        ("autos", False),
        ("", False),
        ("   ", False),
        (None, False),
        (123, False),
    ],
)
def test_is_auto_project_matches_only_the_exact_sentinel(value, expected):
    assert is_auto_project(value) is expected
