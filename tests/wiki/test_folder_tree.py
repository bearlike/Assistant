"""FolderTree — deterministic directory-scaffold synthesis from File paths.

Drives the hierarchy wire mode: assert the exact Folder nodes, folder CONTAINS
edges, and the single ``parentId``/``folderPath`` stamped on every node
(including symbols inheriting their CONTAINS container's id).
"""
from __future__ import annotations

from mewbo_graph.wiki.folder_tree import FolderTree
from mewbo_graph.wiki.types import GraphEdge, GraphNode, make_graph_node

SLUG = "org/repo"


def _file(node_id: str, path: str) -> GraphNode:
    return make_graph_node(
        slug=SLUG, node_id=node_id, type="File", name=path, file=path, range=(0, 10)
    )


def _sym(node_id: str, name: str, path: str, typ: str = "Function") -> GraphNode:
    return make_graph_node(
        slug=SLUG, node_id=node_id, type=typ, name=name, file=path, range=(0, 10)
    )


def _contains(src: str, tgt: str) -> GraphEdge:
    return GraphEdge(slug=SLUG, source=src, target=tgt, type="CONTAINS")


# ── Folder node minting ──────────────────────────────────────────────────────


def test_mints_one_folder_per_unique_prefix() -> None:
    files = [
        _file("f1", "apps/mewbo_api/routes.py"),
        _file("f2", "apps/mewbo_api/jobs.py"),
        _file("f3", "packages/core/loop.py"),
    ]
    tree = FolderTree.build(SLUG, files, [])

    folder_ids = {f.node_id for f in tree.folder_nodes}
    assert folder_ids == {
        "folder:apps",
        "folder:apps/mewbo_api",
        "folder:packages",
        "folder:packages/core",
    }
    # Every folder node is layer-stampable: kind Folder, basename label, path file.
    api = next(f for f in tree.folder_nodes if f.node_id == "folder:apps/mewbo_api")
    assert api.type == "Folder"
    assert api.name == "mewbo_api"
    assert api.file == "apps/mewbo_api"
    assert tree.folder_path_of("folder:apps/mewbo_api") == "apps/mewbo_api"


def test_folder_node_ids_are_deterministic_and_namespaced() -> None:
    files = [_file("f1", "a/b/c.py")]
    tree = FolderTree.build(SLUG, files, [])
    ids = sorted(f.node_id for f in tree.folder_nodes)
    assert ids == ["folder:a", "folder:a/b"]
    # Re-running yields identical output (deterministic).
    again = FolderTree.build(SLUG, files, [])
    assert sorted(f.node_id for f in again.folder_nodes) == ids


# ── Folder CONTAINS edges ────────────────────────────────────────────────────


def test_emits_folder_to_subfolder_and_folder_to_file_edges() -> None:
    files = [_file("f1", "apps/api/routes.py")]
    tree = FolderTree.build(SLUG, files, [])

    edge_pairs = {(e.source, e.target) for e in tree.folder_edges}
    assert ("folder:apps", "folder:apps/api") in edge_pairs  # folder → subfolder
    assert ("folder:apps/api", "f1") in edge_pairs  # folder → file
    assert all(e.type == "CONTAINS" for e in tree.folder_edges)


def test_no_duplicate_folder_edges_for_shared_prefix() -> None:
    files = [
        _file("f1", "apps/api/a.py"),
        _file("f2", "apps/api/b.py"),
        _file("f3", "apps/cli/c.py"),
    ]
    tree = FolderTree.build(SLUG, files, [])
    pairs = [(e.source, e.target) for e in tree.folder_edges]
    # apps → apps/api appears exactly once despite two files under it.
    assert pairs.count(("folder:apps", "folder:apps/api")) == 1


# ── parentId stamping ────────────────────────────────────────────────────────


def test_folder_parent_is_enclosing_folder_root_is_null() -> None:
    files = [_file("f1", "a/b/c.py")]
    tree = FolderTree.build(SLUG, files, [])
    assert tree.parent_of("folder:a") is None  # root folder
    assert tree.parent_of("folder:a/b") == "folder:a"


def test_file_parent_is_its_directory_folder() -> None:
    files = [_file("f1", "apps/api/routes.py")]
    tree = FolderTree.build(SLUG, files, [])
    assert tree.parent_of("f1") == "folder:apps/api"
    assert tree.folder_path_of("f1") == "apps/api/routes.py"


def test_repo_root_file_has_null_parent() -> None:
    files = [_file("f1", "README.py")]
    tree = FolderTree.build(SLUG, files, [])
    assert tree.parent_of("f1") is None
    assert not tree.folder_nodes  # nothing to scaffold at the root
    assert tree.folder_path_of("f1") == "README.py"


def test_symbol_parent_is_its_contains_container() -> None:
    # File f1 CONTAINS class c1; class c1 CONTAINS method m1.
    files = [_file("f1", "a/mod.py")]
    sym_edges = [_contains("f1", "c1"), _contains("c1", "m1")]
    tree = FolderTree.build(SLUG, files, sym_edges)

    assert tree.parent_of("c1") == "f1"  # class → its file
    assert tree.parent_of("m1") == "c1"  # method → its class
    # Symbols carry no folderPath (only Folder + File nodes do).
    assert tree.folder_path_of("c1") is None
    assert tree.folder_path_of("m1") is None


def test_symbol_first_contains_edge_wins_deterministically() -> None:
    files = [_file("f1", "a/mod.py")]
    # Two containers claim the same symbol — first edge in order wins.
    edges = [_contains("f1", "s1"), _contains("other", "s1")]
    tree = FolderTree.build(SLUG, files, edges)
    assert tree.parent_of("s1") == "f1"


def test_external_nodes_are_parentless() -> None:
    files = [_file("f1", "a/mod.py")]
    ext = make_graph_node(
        slug=SLUG, node_id="ext1", type="External", name="os", file="", range=(0, 0)
    )
    tree = FolderTree.build(SLUG, files, [], external_nodes=[ext])
    assert tree.parent_of("ext1") is None
    assert tree.folder_path_of("ext1") is None


def test_file_contains_edge_does_not_override_directory_parent() -> None:
    # A File node must keep its DIRECTORY folder as parent even though it is the
    # SOURCE (never the target) of CONTAINS edges — so no symbol-pass clobber.
    files = [_file("f1", "a/mod.py")]
    edges = [_contains("f1", "c1")]
    tree = FolderTree.build(SLUG, files, edges)
    assert tree.parent_of("f1") == "folder:a"


def test_empty_input_yields_empty_tree() -> None:
    tree = FolderTree.build(SLUG, [], [])
    assert tree.folder_nodes == ()
    assert tree.folder_edges == ()
    assert tree.parents == {}
