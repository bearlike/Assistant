"""Kotlin + Java AST graph coverage (Gitea #189).

Adds `.kt`/`.java` to the tree-sitter language registry alongside the new
Object/Property node kinds and the open `subkind` refinement (schema v2,
#188). Drives `GraphIndex.parse_repo` over the fixture repos from the CALLER
site and asserts exact node/edge/subkind shape, plus one `build_graph_core`
round-trip per language proving the extracted graph passes `CodeGraph`'s
validator — especially that Kotlin's overlapping `class_declaration` query
patterns (a generic "plain class" pattern layered under the data/sealed/enum
subkind-specific ones) never produce a duplicate node_id.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mewbo_graph.wiki.graph import GraphIndex, GraphParseResult
from mewbo_graph.wiki.store import JsonWikiStore
from mewbo_graph.wiki.types import IndexingJob

KOTLIN_FIXTURE = Path(__file__).parent / "fixtures" / "tiny_kotlin_repo"
JAVA_FIXTURE = Path(__file__).parent / "fixtures" / "tiny_java_repo"


@pytest.fixture
def graph():
    return GraphIndex()


def _parse_repo(graph: GraphIndex, fixture: Path, ext: str) -> GraphParseResult:
    files = sorted(fixture.glob(f"*{ext}"))
    return graph.parse_repo(slug="x/y", repo_root=fixture, files=files)


def _build_graph_core(fixture: Path, slug: str, tmp_path, monkeypatch) -> dict:
    """Drive the real ingest path (assemble → ``CodeGraph`` validate → persist)."""
    from mewbo_graph.plugins.wiki import build_graph as bg

    monkeypatch.setattr(bg, "_embeddings_enabled", lambda: False)
    store = JsonWikiStore(root_dir=tmp_path / "wiki")
    store.create_job(
        IndexingJob(
            jobId="j1",
            slug=slug,
            status="queued",
            scannedCount=0,
            totalCount=0,
            currentFile=None,
        )
    )
    ctx = SimpleNamespace(slug=slug, clone_dir=fixture, store=store, job_id="j1")
    result = bg.build_graph_core(ctx)
    return {"result": result, "store": store}


# ── Kotlin ───────────────────────────────────────────────────────────────


def test_kotlin_dot_kt_not_skipped(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    assert result.skipped == []


def test_kotlin_repo_emits_expected_node_kinds(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    kinds = {n.type for n in result.nodes}
    assert kinds >= {
        "File", "Class", "Interface", "Object", "Property", "Function", "Method",
    }


def test_kotlin_class_subkinds(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    subkinds = {n.name: n.subkind for n in result.nodes if n.type == "Class"}
    assert subkinds["User"] == "data_class"
    # Nested inside `sealed class Shape` — the tightest-containment fix
    # (`_subkinds_for`) is what keeps this from being misattributed to Shape.
    assert subkinds["Circle"] == "data_class"
    assert subkinds["Shape"] == "sealed_class"
    assert subkinds["Mode"] == "enum_class"
    assert subkinds["HomeScreen"] is None  # plain class stays unrefined


def test_kotlin_object_and_companion_subkind(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    objects = {n.name: n for n in result.nodes if n.type == "Object" and n.subkind is None}
    assert "Empty" in objects  # plain `object Empty : Shape()` — no subkind

    companions = [
        n for n in result.nodes if n.type == "Object" and n.subkind == "companion_object"
    ]
    # Registry's and HomeScreen's companion objects are both anonymous —
    # `_pair_defs_with_names` falls back both to the literal "Companion".
    assert len(companions) == 2
    assert all(n.name == "Companion" for n in companions)


def test_kotlin_nested_named_object_inside_anonymous_companion(graph, tmp_path):
    """A named object nested inside an anonymous companion must NOT swap names.

    Regression: `_pair_defs_with_names` used to walk `names` with a single
    left-to-right pointer, so the OUTER (anonymous companion) def — whose
    wider byte range also contains the INNER def's own name token — grabbed
    "Inner" first, leaving the inner object to fall back to "Companion". The
    fix picks the TIGHTEST containing def per name token (same algorithm as
    `_subkinds_for`), so nesting can no longer swap names between two real
    nodes.
    """
    src = tmp_path / "nested.kt"
    src.write_text(
        "class Registry {\n"
        "    companion object {\n"
        "        object Inner { const val X = 1 }\n"
        "    }\n"
        "}\n",
        encoding="utf-8",
    )
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    objects = {n.name: n for n in result.nodes if n.type == "Object"}
    assert set(objects) == {"Companion", "Inner"}
    assert objects["Companion"].subkind == "companion_object"
    assert objects["Inner"].subkind is None
    # The outer (companion) range must strictly ENCLOSE the inner object's —
    # confirms the ranges themselves weren't swapped along with the names.
    outer, inner = objects["Companion"].range, objects["Inner"].range
    assert outer[0] < inner[0] and inner[1] < outer[1]


def test_kotlin_property_nodes_top_level_and_member_only(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    names = {n.name for n in result.nodes if n.type == "Property"}
    # VERSION/defaultMode/title/mode are plain property_declaration; id/name
    # (User) and radius (Circle) are PRIMARY-CONSTRUCTOR val/var properties
    # (`class_parameter`, a different grammar node — see
    # test_kotlin_primary_constructor_properties); greeter is HomeScreen's
    # constructor-promoted property.
    assert names == {
        "VERSION", "defaultMode", "title", "mode", "id", "name", "radius", "greeter",
    }
    # `val handler = { ... }` inside compose.kt's function body is a LOCAL
    # variable, not a field — must not become a graph node.
    assert "handler" not in names


def test_kotlin_primary_constructor_properties(graph, tmp_path):
    """A plain (non-val/var) constructor parameter must NOT become a Property."""
    src = tmp_path / "ctor.kt"
    src.write_text(
        "class Point(val x: Int, var y: Int, plain: Boolean)\n", encoding="utf-8"
    )
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    names = {n.name for n in result.nodes if n.type == "Property"}
    assert names == {"x", "y"}
    assert "plain" not in names


def test_kotlin_enum_class_members_after_semicolon(graph, tmp_path):
    """Methods/properties declared after `;` in an enum class body are captured.

    `enum class`'s body is a DISTINCT `enum_class_body` node, not the plain
    `class_body` the ordinary method/property patterns scope to.
    """
    src = tmp_path / "mode.kt"
    src.write_text(
        "enum class Mode {\n"
        "    LIGHT, DARK;\n"
        "    fun label() = name.lowercase()\n"
        "    val tag: String = \"mode\"\n"
        "}\n",
        encoding="utf-8",
    )
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    assert {n.name for n in result.nodes if n.type == "Method"} == {"label"}
    assert {n.name for n in result.nodes if n.type == "Property"} == {"tag"}


def test_kotlin_qualified_supertype_captures_only_final_segment(graph, tmp_path):
    """A package-qualified supertype (`pkg.sub.Base()`) must EXTENDS to "Base" only.

    `user_type` is a FLAT node (one `type_identifier` child per dotted
    segment) — an unanchored capture would grab "pkg"/"sub"/"Base" as three
    separate (wrong) superclass.name matches.
    """
    src = tmp_path / "qualified.kt"
    src.write_text("class Foo : pkg.sub.Base()\n", encoding="utf-8")
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    extends = [e for e in result.edges if e.type == "EXTENDS"]
    assert len(extends) == 1
    assert extends[0].target_name == "Base"


def test_kotlin_local_object_is_not_a_top_level_node(graph, tmp_path):
    """An `object` declared inside a function body must NOT become a graph node.

    Scoped identically to function.def/property.def (source_file/class_body
    direct children only) — a local object is neither.
    """
    src = tmp_path / "local_obj.kt"
    src.write_text(
        "fun use(): Int {\n"
        "    object Local {\n"
        "        val x = 1\n"
        "    }\n"
        "    return Local.x\n"
        "}\n",
        encoding="utf-8",
    )
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    assert {n.name for n in result.nodes if n.type == "Object"} == set()


def test_kotlin_extension_function_references_edge(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    shout = next(n for n in result.nodes if n.type == "Function" and n.name == "shout")
    ext_edges = [
        e
        for e in result.edges
        if e.type == "REFERENCES" and e.attributes.get("kotlin.extension") is True
    ]
    assert len(ext_edges) == 1
    edge = ext_edges[0]
    # CRITICAL regression guard: the source id must equal the PERSISTED
    # Function node's id — computed from the def's byte, not the name
    # token's — mirroring (but not replicating) the pre-existing EXTENDS
    # block's dangling-source bug.
    assert edge.source == shout.node_id
    assert edge.target_name == "String"


def test_kotlin_repo_has_no_duplicate_node_ids(graph):
    result = _parse_repo(graph, KOTLIN_FIXTURE, ".kt")
    ids = [n.node_id for n in result.nodes]
    assert len(ids) == len(set(ids))


def test_build_graph_core_validates_kotlin_fixture(tmp_path, monkeypatch):
    outcome = _build_graph_core(
        KOTLIN_FIXTURE, "example.com/o/kt", tmp_path, monkeypatch
    )
    assert outcome["result"]["nodeCount"] > 0
    nodes = outcome["store"].query_graph("example.com/o/kt")
    assert any(n.type == "Object" for n in nodes)
    assert any(n.subkind == "data_class" for n in nodes)


# ── Java ─────────────────────────────────────────────────────────────────


def test_java_dot_java_not_skipped(graph):
    result = _parse_repo(graph, JAVA_FIXTURE, ".java")
    assert result.skipped == []


def test_java_repo_emits_expected_node_kinds(graph):
    result = _parse_repo(graph, JAVA_FIXTURE, ".java")
    kinds = {n.type for n in result.nodes}
    assert kinds >= {"File", "Class", "Interface", "Method", "Property"}


def test_java_class_subkinds(graph):
    result = _parse_repo(graph, JAVA_FIXTURE, ".java")
    subkinds = {n.name: n.subkind for n in result.nodes if n.type == "Class"}
    assert subkinds["Mode"] == "enum"
    assert subkinds["User"] == "record"
    assert subkinds["HomeScreen"] is None
    assert subkinds["Singleton"] is None


def test_java_repo_edges(graph):
    result = _parse_repo(graph, JAVA_FIXTURE, ".java")
    imports = [e for e in result.edges if e.type == "IMPORTS"]
    calls = [e for e in result.edges if e.type == "CALLS"]
    extends = [e for e in result.edges if e.type == "EXTENDS"]
    assert len(imports) >= 1
    assert len(calls) >= 2
    # java.scm deliberately does NOT capture `implements` at all (same
    # extends-only precedent as typescript.scm) — Greeter never contributes a
    # superclass.name capture, so there's nothing to misalign across classes.
    assert len(extends) == 1
    assert extends[0].target_name == "BaseScreen"


def test_java_qualified_extends_captures_final_segment(graph, tmp_path):
    """`extends pkg.sub.Base` (no import, fully qualified) must EXTENDS to "Base".

    The qualified form is a recursively-nested `scoped_type_identifier`
    (unlike the flat `type_identifier` for a bare `extends Base`) — anchoring
    the innermost/last `type_identifier` handles any qualifier depth.
    """
    src = tmp_path / "Foo.java"
    src.write_text("class Foo extends pkg.sub.Base {\n}\n", encoding="utf-8")
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    extends = [e for e in result.edges if e.type == "EXTENDS"]
    assert len(extends) == 1
    assert extends[0].target_name == "Base"


def test_java_multiple_heritage_classes_do_not_cross_contaminate(graph, tmp_path):
    """Two heritage-bearing classes in one file must each keep their OWN target.

    Regression guard: java.scm used to ALSO capture `implements`, which for
    "extends X implements Y" pushed the subclass:superclass ratio to 1:2 for
    the single most common Java class shape — the shared per-file 1:1 zip in
    `graph.py` would then misalign every subsequent heritage-bearing class's
    edge (this file's ``B`` would have wrongly EXTENDS'd to "Greeter").
    """
    src = tmp_path / "Multi.java"
    src.write_text(
        "class HomeScreen extends BaseScreen implements Greeter {\n}\n"
        "class B extends Other {\n}\n",
        encoding="utf-8",
    )
    result = graph.parse_file(slug="x/y", file_path=src, repo_root=tmp_path)
    extends = {e.target_name for e in result.edges if e.type == "EXTENDS"}
    assert extends == {"BaseScreen", "Other"}


def test_build_graph_core_validates_java_fixture(tmp_path, monkeypatch):
    outcome = _build_graph_core(
        JAVA_FIXTURE, "example.com/o/java", tmp_path, monkeypatch
    )
    assert outcome["result"]["nodeCount"] > 0
    nodes = outcome["store"].query_graph("example.com/o/java")
    assert any(n.subkind == "record" for n in nodes)
    assert any(n.subkind == "enum" for n in nodes)
