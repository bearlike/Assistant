#!/usr/bin/env python3
"""Virtual project storage and management."""

from __future__ import annotations

import abc
import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jinja2

from mewbo_core.common import get_logger
from mewbo_core.config import get_config
from mewbo_core.worktree import WorktreeManager, slugify_branch

logging = get_logger(name="core.project_store")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class VirtualProject:
    """A virtual project workspace with metadata and filesystem path.

    A worktree is modeled as a child ``VirtualProject`` with ``is_worktree=True``
    and ``parent_project_id``/``branch`` populated. Its ``project_id`` follows
    the deterministic format ``wt:<parent_id>:<slugified-branch>`` so that the
    same branch always resolves to the same record.
    """

    project_id: str
    name: str
    description: str
    created_at: str
    updated_at: str
    path: str
    path_source: str = "auto"  # "auto" | "provided"
    folder_created: bool = True
    # Worktree extension (None for regular managed projects).
    parent_project_id: str | None = None
    branch: str | None = None
    is_worktree: bool = False


def worktree_project_id(parent_project_id: str, branch: str) -> str:
    """Deterministic project_id for a worktree row."""
    return f"wt:{parent_project_id}:{slugify_branch(branch)}"


def _render_claude_md(name: str, description: str) -> str:
    template_path = Path(__file__).parent / "templates" / "project_claude_md.j2"
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(template_path.parent)), autoescape=False
    )
    return env.get_template("project_claude_md.j2").render(name=name, description=description)


def _setup_project_folder(path: Path, name: str, description: str) -> bool:
    """Create folder and CLAUDE.md if needed. Returns folder_created."""
    folder_created = not path.exists()
    path.mkdir(parents=True, exist_ok=True)
    claude_md = path / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(_render_claude_md(name, description))
    return folder_created


class ProjectStoreBase(abc.ABC):
    """Abstract base for virtual project storage backends."""

    @abc.abstractmethod
    def create_project(
        self, name: str, description: str, path: str | None = None
    ) -> VirtualProject:
        """Create a new virtual project."""

    @abc.abstractmethod
    def list_projects(self) -> list[VirtualProject]:
        """List all virtual projects."""

    @abc.abstractmethod
    def get_project(self, project_id: str) -> VirtualProject | None:
        """Get a project by ID, or None if not found."""

    @abc.abstractmethod
    def update_project(
        self, project_id: str, name: str | None = None, description: str | None = None
    ) -> VirtualProject:
        """Update a project's name or description."""

    @abc.abstractmethod
    def delete_project(self, project_id: str) -> None:
        """Delete a project by ID."""

    # ------------------------------------------------------------------
    # Worktree-specific helpers (default implementations on top of CRUD).
    # ------------------------------------------------------------------

    def list_worktrees(self, parent_project_id: str) -> list[VirtualProject]:
        """List worktree rows for *parent_project_id*."""
        return [
            p
            for p in self.list_projects()
            if p.is_worktree and p.parent_project_id == parent_project_id
        ]

    def create_worktree(self, parent_project_id: str, branch: str) -> VirtualProject:
        """Create a worktree-backed VirtualProject for *branch* under *parent*.

        Idempotent: if the deterministic worktree project_id already exists,
        returns the existing record (after verifying its path still exists).
        """
        parent = self.get_project(parent_project_id)
        if parent is None:
            raise KeyError(f"Parent project {parent_project_id} not found")

        wt_id = worktree_project_id(parent_project_id, branch)
        existing = self.get_project(wt_id)
        if existing is not None and Path(existing.path).exists():
            return existing
        if existing is not None:
            # Stale record — drop and recreate.
            self.delete_project(wt_id)

        path = WorktreeManager.create(parent.path, branch)
        return self._persist_worktree(
            project_id=wt_id,
            parent_project_id=parent_project_id,
            branch=branch,
            path=path,
        )

    def delete_worktree(self, project_id: str, *, force: bool = False) -> None:
        """Remove the git worktree and the persisted record.

        Raises ``RuntimeError`` if the worktree is dirty and ``force`` is False.
        """
        proj = self.get_project(project_id)
        if proj is None or not proj.is_worktree:
            raise KeyError(f"Worktree {project_id} not found")
        WorktreeManager.remove(proj.path, force=force)
        self.delete_project(project_id)

    @abc.abstractmethod
    def _persist_worktree(
        self,
        *,
        project_id: str,
        parent_project_id: str,
        branch: str,
        path: str,
    ) -> VirtualProject:
        """Persist a worktree row. Backend-specific."""


class JsonProjectStore(ProjectStoreBase):
    """JSON file-backed project store."""

    def __init__(self) -> None:  # noqa: D107
        config = get_config()
        self._projects_home = Path(config.runtime.projects_home)
        self._data_file = (
            Path(config.runtime.config_dir or str(Path.home() / ".mewbo"))
            / "virtual_projects.json"
        )
        self._lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        if not self._data_file.exists():
            return []
        try:
            return json.loads(self._data_file.read_text())
        except Exception:
            return []

    def _save(self, records: list[dict[str, Any]]) -> None:
        self._data_file.parent.mkdir(parents=True, exist_ok=True)
        self._data_file.write_text(json.dumps(records, indent=2, default=str))

    def _to_project(self, d: dict[str, Any]) -> VirtualProject:
        return VirtualProject(
            project_id=d["project_id"],
            name=d["name"],
            description=d.get("description", ""),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            path=d["path"],
            path_source=d.get("path_source", "auto"),
            folder_created=d.get("folder_created", True),
            parent_project_id=d.get("parent_project_id"),
            branch=d.get("branch"),
            is_worktree=d.get("is_worktree", False),
        )

    def create_project(  # noqa: D102
        self, name: str, description: str, path: str | None = None
    ) -> VirtualProject:
        project_id = str(uuid.uuid4())
        now = _utc_now()
        if path:
            p = Path(path)
            path_source = "provided"
        else:
            p = self._projects_home / project_id
            path_source = "auto"
        folder_created = _setup_project_folder(p, name, description)
        proj = VirtualProject(
            project_id=project_id,
            name=name,
            description=description,
            created_at=now,
            updated_at=now,
            path=str(p),
            path_source=path_source,
            folder_created=folder_created,
        )
        with self._lock:
            records = self._load()
            records.append(asdict(proj))
            self._save(records)
        return proj

    def _persist_worktree(  # noqa: D102
        self,
        *,
        project_id: str,
        parent_project_id: str,
        branch: str,
        path: str,
    ) -> VirtualProject:
        now = _utc_now()
        proj = VirtualProject(
            project_id=project_id,
            name=branch,
            description=f"Worktree on branch '{branch}'",
            created_at=now,
            updated_at=now,
            path=path,
            path_source="auto",
            folder_created=True,
            parent_project_id=parent_project_id,
            branch=branch,
            is_worktree=True,
        )
        with self._lock:
            records = self._load()
            records.append(asdict(proj))
            self._save(records)
        return proj

    def list_projects(self) -> list[VirtualProject]:  # noqa: D102
        with self._lock:
            return [self._to_project(d) for d in self._load()]

    def get_project(self, project_id: str) -> VirtualProject | None:  # noqa: D102
        with self._lock:
            for d in self._load():
                if d["project_id"] == project_id:
                    return self._to_project(d)
        return None

    def update_project(  # noqa: D102
        self, project_id: str, name: str | None = None, description: str | None = None
    ) -> VirtualProject:
        with self._lock:
            records = self._load()
            for d in records:
                if d["project_id"] == project_id:
                    if name is not None:
                        d["name"] = name
                    if description is not None:
                        d["description"] = description
                    d["updated_at"] = _utc_now()
                    self._save(records)
                    return self._to_project(d)
        raise KeyError(f"Project {project_id} not found")

    def delete_project(self, project_id: str) -> None:  # noqa: D102
        with self._lock:
            records = self._load()
            remaining = [d for d in records if d["project_id"] != project_id]
            deleted = [d for d in records if d["project_id"] == project_id]
            if (
                deleted
                and deleted[0].get("path_source", "auto") == "auto"
                and not deleted[0].get("is_worktree", False)
            ):
                p = Path(deleted[0]["path"])
                if p.exists():
                    p.rename(str(p) + ".deleted")
            self._save(remaining)


class MongoProjectStore(ProjectStoreBase):
    """MongoDB-backed project store."""

    def __init__(self, mongodb_uri: str, database: str = "mewbo") -> None:  # noqa: D107
        from pymongo import ASCENDING, MongoClient

        self._client: MongoClient = MongoClient(mongodb_uri)
        self._col = self._client[database]["virtual_projects"]
        self._col.create_index([("project_id", ASCENDING)], unique=True)
        config = get_config()
        self._projects_home = Path(config.runtime.projects_home)

    def _to_project(self, d: dict[str, Any]) -> VirtualProject:
        return VirtualProject(
            project_id=d["project_id"],
            name=d["name"],
            description=d.get("description", ""),
            created_at=str(d["created_at"]),
            updated_at=str(d["updated_at"]),
            path=d["path"],
            path_source=d.get("path_source", "auto"),
            folder_created=d.get("folder_created", True),
            parent_project_id=d.get("parent_project_id"),
            branch=d.get("branch"),
            is_worktree=d.get("is_worktree", False),
        )

    def create_project(  # noqa: D102
        self, name: str, description: str, path: str | None = None
    ) -> VirtualProject:
        project_id = str(uuid.uuid4())
        now = _utc_now()
        if path:
            p = Path(path)
            path_source = "provided"
        else:
            p = self._projects_home / project_id
            path_source = "auto"
        folder_created = _setup_project_folder(p, name, description)
        doc: dict[str, Any] = {
            "project_id": project_id,
            "name": name,
            "description": description,
            "created_at": now,
            "updated_at": now,
            "path": str(p),
            "path_source": path_source,
            "folder_created": folder_created,
        }
        self._col.insert_one(doc)
        return self._to_project(doc)

    def list_projects(self) -> list[VirtualProject]:  # noqa: D102
        return [self._to_project(d) for d in self._col.find({}, {"_id": 0})]

    def get_project(self, project_id: str) -> VirtualProject | None:  # noqa: D102
        d = self._col.find_one({"project_id": project_id}, {"_id": 0})
        return self._to_project(d) if d else None

    def update_project(  # noqa: D102
        self, project_id: str, name: str | None = None, description: str | None = None
    ) -> VirtualProject:
        update: dict[str, Any] = {"updated_at": _utc_now()}
        if name is not None:
            update["name"] = name
        if description is not None:
            update["description"] = description
        result = self._col.find_one_and_update(
            {"project_id": project_id},
            {"$set": update},
            return_document=True,
            projection={"_id": 0},
        )
        if not result:
            raise KeyError(f"Project {project_id} not found")
        return self._to_project(result)

    def delete_project(self, project_id: str) -> None:  # noqa: D102
        d = self._col.find_one({"project_id": project_id}, {"_id": 0})
        if (
            d
            and d.get("path_source", "auto") == "auto"
            and not d.get("is_worktree", False)
        ):
            p = Path(d["path"])
            if p.exists():
                p.rename(str(p) + ".deleted")
        self._col.delete_one({"project_id": project_id})

    def _persist_worktree(  # noqa: D102
        self,
        *,
        project_id: str,
        parent_project_id: str,
        branch: str,
        path: str,
    ) -> VirtualProject:
        now = _utc_now()
        doc: dict[str, Any] = {
            "project_id": project_id,
            "name": branch,
            "description": f"Worktree on branch '{branch}'",
            "created_at": now,
            "updated_at": now,
            "path": path,
            "path_source": "auto",
            "folder_created": True,
            "parent_project_id": parent_project_id,
            "branch": branch,
            "is_worktree": True,
        }
        self._col.insert_one(doc)
        return self._to_project(doc)


def create_project_store() -> ProjectStoreBase:
    """Return the configured project store (MongoDB or JSON)."""
    config = get_config()
    if config.storage.driver == "mongodb" and config.storage.mongodb:
        mongo = config.storage.mongodb
        return MongoProjectStore(mongo.uri, mongo.database)
    return JsonProjectStore()
