"""GraphIndex tests — Python tree-sitter parsing."""
from pathlib import Path

import pytest
from mewbo_graph.wiki.graph import GraphIndex, GraphParseResult

FIXTURE = Path(__file__).parent / "fixtures" / "tiny_python_repo"

LANG_FIXTURES: dict[str, tuple[Path, str]] = {
    "javascript": (FIXTURE.parent / "tiny_js_repo", "lib.js"),
    "typescript": (FIXTURE.parent / "tiny_ts_repo", "lib.ts"),
    "go":         (FIXTURE.parent / "tiny_go_repo",  "lib.go"),
    "rust":       (FIXTURE.parent / "tiny_rust_repo", "lib.rs"),
}


@pytest.fixture
def graph():
    return GraphIndex()


def test_parse_file_emits_class_function_method_nodes(graph):
    result = graph.parse_file(slug="x/y", file_path=FIXTURE / "core.py", repo_root=FIXTURE)
    assert isinstance(result, GraphParseResult)
    # 1 File + 1 Class + 2 Functions + 2 Methods = 6 nodes
    type_counts: dict[str, int] = {}
    for n in result.nodes:
        type_counts[n.type] = type_counts.get(n.type, 0) + 1
    assert type_counts["File"] == 1
    assert type_counts["Class"] == 1
    assert type_counts["Function"] == 2
    assert type_counts["Method"] == 2


def test_parse_file_emits_contains_edges(graph):
    result = graph.parse_file(slug="x/y", file_path=FIXTURE / "core.py", repo_root=FIXTURE)
    contains = [e for e in result.edges if e.type == "CONTAINS"]
    # File→Class, File→Function (×2), File→Method (×2) = 5 CONTAINS at minimum
    assert len(contains) >= 3


def test_parse_file_emits_imports_and_calls(graph):
    result = graph.parse_file(slug="x/y", file_path=FIXTURE / "main.py", repo_root=FIXTURE)
    imports = [e for e in result.edges if e.type == "IMPORTS"]
    calls = [e for e in result.edges if e.type == "CALLS"]
    # `import core` + `from utils import normalize` = 2 imports
    assert len(imports) >= 2
    # `core.run_engine()` and `normalize(str(result))` and `str(...)` = >= 2 calls
    assert len(calls) >= 2


def test_parse_file_emits_extends_edge(graph):
    result = graph.parse_file(slug="x/y", file_path=FIXTURE / "utils.py", repo_root=FIXTURE)
    extends = [e for e in result.edges if e.type == "EXTENDS"]
    # StringUtil(Engine)
    assert len(extends) == 1


def test_parse_repo_aggregates_all_files(graph):
    files = sorted([p for p in FIXTURE.rglob("*.py") if p.is_file()])
    result = graph.parse_repo(slug="x/y", repo_root=FIXTURE, files=files)
    # 3 files → 3 File nodes minimum
    file_nodes = [n for n in result.nodes if n.type == "File"]
    assert len(file_nodes) == 3


def test_parse_repo_skips_non_python(tmp_path, graph):
    (tmp_path / "a.txt").write_text("hello")
    (tmp_path / "b.py").write_text("def f(): pass\n")
    result = graph.parse_repo(slug="x/y", repo_root=tmp_path, files=list(tmp_path.iterdir()))
    assert "a.txt" in result.skipped
    assert all(n.file != "a.txt" for n in result.nodes)


def test_extract_docstring_for_function(graph):
    result = graph.parse_file(slug="x/y", file_path=FIXTURE / "core.py", repo_root=FIXTURE)
    fn = next((n for n in result.nodes if n.type == "Function" and n.name == "run_engine"), None)
    assert fn is not None
    assert fn.docstring is not None
    assert "Entry-point" in fn.docstring


def test_stable_node_id_deterministic(graph):
    r1 = graph.parse_file(slug="x/y", file_path=FIXTURE / "core.py", repo_root=FIXTURE)
    r2 = graph.parse_file(slug="x/y", file_path=FIXTURE / "core.py", repo_root=FIXTURE)
    ids1 = sorted([n.node_id for n in r1.nodes])
    ids2 = sorted([n.node_id for n in r2.nodes])
    assert ids1 == ids2


# ── multi-language tests ───────────────────────────────────────────────────────


@pytest.mark.parametrize("lang", list(LANG_FIXTURES.keys()))
def test_parse_file_per_language_emits_expected_node_kinds(graph, lang):
    root, fname = LANG_FIXTURES[lang]
    result = graph.parse_file(slug="x/y", file_path=root / fname, repo_root=root)
    type_counts: dict[str, int] = {}
    for n in result.nodes:
        type_counts[n.type] = type_counts.get(n.type, 0) + 1
    assert type_counts.get("File", 0) == 1
    if lang in {"javascript", "typescript"}:
        assert type_counts.get("Class", 0) >= 1
    if lang in {"typescript", "go", "rust"}:
        assert type_counts.get("Interface", 0) >= 1
    if lang in {"go", "rust"}:
        assert type_counts.get("Class", 0) >= 1  # struct → Class


@pytest.mark.parametrize("lang", list(LANG_FIXTURES.keys()))
def test_parse_file_emits_imports_per_language(graph, lang):
    root, fname = LANG_FIXTURES[lang]
    result = graph.parse_file(slug="x/y", file_path=root / fname, repo_root=root)
    imports = [e for e in result.edges if e.type == "IMPORTS"]
    assert len(imports) >= 1


def test_parse_file_skips_unsupported_extension(graph, tmp_path):
    f = tmp_path / "x.md"
    f.write_text("# nothing here")
    result = graph.parse_file(slug="x/y", file_path=f, repo_root=tmp_path)
    assert result.skipped == ["x.md"]
    assert result.nodes == []
