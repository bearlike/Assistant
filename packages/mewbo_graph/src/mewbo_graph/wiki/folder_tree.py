"""Directory-hierarchy scaffold for the knowledge-graph viewer.

``FolderTree`` synthesises a folder scaffold from the File nodes' ``file``
paths so the viewer can cluster + collapse by directory (the "hierarchy" wire
mode). It is a VIEW-time concern only — folders are never persisted; the
extractor emits real in-repo nodes and ``CONTAINS`` edges, and this class
derives the directory tree from those path strings deterministically.

It owns three outputs over its input File nodes + the resolved AST edges:

* ``folder_nodes`` — one synthesised ``Folder`` ``GraphNode`` per unique
  directory prefix (id ``folder:<path>``, ``layer=ast``).
* ``folder_edges`` — ``CONTAINS`` edges folder→subfolder and folder→file.
* ``parents`` — a single deterministic ``parentId`` for every node id in the
  graph (folder→parent folder, file→its directory folder, symbol→its container
  via the inbound ``CONTAINS`` edge, External→null).

Path decomposition uses ``pathlib.PurePosixPath`` (repo paths are always
'/'-separated); there is no hand-rolled string splitting.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from .types import FolderNode, GraphEdge, GraphNode

if TYPE_CHECKING:
    from collections.abc import Iterable

# Synthetic Folder node ids are namespaced so they never collide with the
# content-addressed sha1 ids the extractor mints for real symbols.
_FOLDER_PREFIX = "folder:"


@dataclass(frozen=True, slots=True)
class FolderTree:
    """Synthesised directory scaffold + single-parent map for the viewer.

    ``parents`` maps EVERY node id seen at build time to its one deterministic
    ``parentId`` (``None`` for roots / parentless nodes). The folder nodes +
    edges are view-only ``GraphNode``/``GraphEdge`` instances carrying the same
    ``slug`` as the graph so they round-trip through the existing serialiser.
    """

    slug: str
    folder_nodes: tuple[GraphNode, ...]
    folder_edges: tuple[GraphEdge, ...]
    parents: dict[str, str | None]
    folder_paths: dict[str, str]  # node_id → folderPath (Folder + File nodes)

    # ── Construction ────────────────────────────────────────────────────

    @classmethod
    def build(
        cls,
        slug: str,
        file_nodes: Iterable[GraphNode],
        edges: Iterable[GraphEdge],
        *,
        external_nodes: Iterable[GraphNode] = (),
    ) -> FolderTree:
        """Derive the folder scaffold + parent map for *slug*.

        ``file_nodes`` are the File ``GraphNode``s (their ``file`` path drives
        the directory tree). ``edges`` are the RESOLVED AST edges — the inbound
        ``CONTAINS`` edge to a symbol names that symbol's container, which is
        its ``parentId``. ``external_nodes`` are the synthesised convergence
        nodes (always parentless). Symbol nodes are read off the edge targets,
        so the caller need not pass them separately.
        """
        files = [n for n in file_nodes if n.type == "File"]

        folder_nodes: dict[str, GraphNode] = {}
        folder_edges: list[GraphEdge] = []
        parents: dict[str, str | None] = {}
        folder_paths: dict[str, str] = {}

        def _folder_id(p: PurePosixPath) -> str:
            return _FOLDER_PREFIX + p.as_posix()

        def _add_contains(source: str, target: str) -> None:
            folder_edges.append(
                GraphEdge(slug=slug, source=source, target=target, type="CONTAINS")
            )

        for fnode in files:
            file_path = PurePosixPath(fnode.file)
            folder_paths[fnode.node_id] = fnode.file
            parent_dir = file_path.parent  # PurePosixPath('.') for a repo-root file
            is_root_file = parent_dir == PurePosixPath(".")
            parents[fnode.node_id] = None if is_root_file else _folder_id(parent_dir)

            # Mint a Folder for the file's directory and every ancestor of it,
            # chaining each to its parent (folder→subfolder + folder→file edges).
            # ``parents`` excludes '.', so the shallowest folder lands parentless.
            ancestry = [] if is_root_file else [parent_dir, *parent_dir.parents]
            for path in ancestry:
                if path == PurePosixPath("."):
                    continue
                fid = _folder_id(path)
                if fid not in folder_nodes:
                    folder_nodes[fid] = FolderNode(
                        slug=slug,
                        node_id=fid,
                        name=path.name,
                        file=path.as_posix(),
                        range=(0, 0),
                        docstring=None,
                    )
                    folder_paths[fid] = path.as_posix()
                    grandparent = path.parent
                    parent_id = (
                        None
                        if grandparent == PurePosixPath(".")
                        else _folder_id(grandparent)
                    )
                    parents[fid] = parent_id
                    if parent_id is not None:
                        _add_contains(parent_id, fid)
            if not is_root_file:
                _add_contains(_folder_id(parent_dir), fnode.node_id)

        # Parent every symbol at its CONTAINS container (File→symbol,
        # Class→method). First inbound edge wins, so a deterministic edge order
        # yields a deterministic parent; symbols without one stay null. A File
        # is only ever a CONTAINS *source*, so its directory parent is safe.
        for e in edges:
            if e.type == "CONTAINS" and e.target not in parents:
                parents[e.target] = e.source

        # External convergence nodes are always parentless.
        for ext in external_nodes:
            parents.setdefault(ext.node_id, None)

        return cls(
            slug=slug,
            folder_nodes=tuple(folder_nodes.values()),
            folder_edges=tuple(folder_edges),
            parents=parents,
            folder_paths=folder_paths,
        )

    # ── Derived accessors ───────────────────────────────────────────────

    def parent_of(self, node_id: str) -> str | None:
        """Return the deterministic ``parentId`` for *node_id* (None if none)."""
        return self.parents.get(node_id)

    def folder_path_of(self, node_id: str) -> str | None:
        """Return the ``folderPath`` for a Folder/File node, else None."""
        return self.folder_paths.get(node_id)
