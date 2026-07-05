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


# ── capture-pairing regression (names must bind to their OWN def) ───────────────
#
# ``QueryCursor.captures()`` groups matches per capture NAME, but the per-name
# lists are NOT guaranteed to be mutually index-aligned — their order varies
# run-to-run. ``_extract`` used to ``zip(captures["class.def"], captures
# ["class.name"])``, which silently attached a node's name to the WRONG def's
# byte range whenever the two lists came back in different orders (corrupting the
# graph + any downstream resolver that matches on name). The fix orders each list
# by start byte before zipping; this test forces the worst-case misalignment
# (defs descending, names ascending) so it fails deterministically without it.

_PAIR_SRC = b"""\
class Base:
    def run(self):
        return 1


class Mid:
    def step(self):
        return 2


class Alpha(Base):
    def run(self):
        return 3


class Beta(Mid):
    def go(self):
        return 4
"""


def _misaligned_python_captures(source: bytes) -> dict:
    """Real python captures with each def/name pair forced into OPPOSITE orders.

    Deterministically reproduces the nondeterministic real-world misalignment:
    every ``*.def`` (and ``superclass.name``) list is reversed relative to its
    partner, so an order-naive ``zip`` mispairs every node.
    """
    import tree_sitter_language_pack as tlp
    from tree_sitter import Parser, Query, QueryCursor

    lang = tlp.get_language("python")
    scm = (
        Path(__file__).parents[2]
        / "packages/mewbo_graph/src/mewbo_graph/wiki/graph_queries/python.scm"
    ).read_text(encoding="utf-8")
    caps = QueryCursor(Query(lang, scm)).captures(Parser(lang).parse(source).root_node)
    asc = lambda nodes: sorted(nodes, key=lambda n: n.start_byte)  # noqa: E731
    desc = lambda nodes: sorted(nodes, key=lambda n: -n.start_byte)  # noqa: E731
    for def_key, name_key in (
        ("class.def", "class.name"),
        ("function.def", "function.name"),
        ("method.def", "method.name"),
    ):
        if def_key in caps:
            caps[def_key] = desc(caps[def_key])
            caps[name_key] = asc(caps[name_key])
    if "superclass.name" in caps:
        caps["superclass.name"] = desc(caps["superclass.name"])
        caps["subclass.name"] = asc(caps["subclass.name"])
    return caps


def test_extract_binds_names_to_own_def_under_misaligned_captures():
    pytest.importorskip("tree_sitter_language_pack")
    from mewbo_graph.wiki.graph import _extract, _stable_id

    caps = _misaligned_python_captures(_PAIR_SRC)
    result = _extract("s/r", "m.py", _PAIR_SRC, caps)

    # Every Class/Method/Function node's NAME must match the source at its range.
    classes = {n.name: n for n in result.nodes if n.type == "Class"}
    assert set(classes) == {"Base", "Mid", "Alpha", "Beta"}
    callables = [n for n in result.nodes if n.type in ("Class", "Method", "Function")]
    for n in callables:
        head = _PAIR_SRC[n.range[0] : n.range[1]]
        kw = b"class " if n.type == "Class" else b"def "
        assert head.startswith(kw + n.name.encode()), (
            f"{n.type} {n.name!r} bound to wrong range: {head[:20]!r}"
        )

    # EXTENDS must pair the right subclass with the right superclass.
    extends = {
        e.source: e.target_name for e in result.edges if e.type == "EXTENDS"
    }
    alpha = _stable_id("s/r", "Class", "Alpha", "m.py", _PAIR_SRC.index(b"Alpha"))
    beta = _stable_id("s/r", "Class", "Beta", "m.py", _PAIR_SRC.index(b"Beta"))
    assert extends.get(alpha) == "Base"
    assert extends.get(beta) == "Mid"


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
