"""``ScipPythonResolver`` — faithful Python resolution over real tree-sitter nodes.

The unit tests stub only the I/O boundary (scip itself, via an injected
producer): the graph nodes come from the REAL ``GraphIndex`` extractor and the
resolver re-reads the on-disk source bytes, so the byte-vs-line:col mapping,
containment match, descriptor stitching, ambiguity drop, and External
convergence are all exercised end-to-end. Crafted SCIP occurrences mirror the
exact wire shape captured from the spike (``spike.json``). One integration
test runs the real scip-python + scip binaries when present.
"""
from __future__ import annotations

import ast
import shutil
from pathlib import Path
from typing import Any

import pytest
from mewbo_graph.wiki.graph import _stable_id
from mewbo_graph.wiki.resolve import (
    ResolutionResult,
    ScipPythonResolver,
    SubprocessScipProducer,
)
from mewbo_graph.wiki.types import GraphNode, make_graph_node

SLUG = "host/org/repo"


# ── SCIP wire-shape builders (match scip print --json) ──────────────────────


def _loc(text: str, token: str, n: int = 0) -> list[int]:
    """0-based ``[line, start_col, end_col]`` of the *n*-th *token* in *text*."""
    idx = -1
    for _ in range(n + 1):
        idx = text.index(token, idx + 1)
    line0 = text.count("\n", 0, idx)
    col0 = idx - (text.rfind("\n", 0, idx) + 1)
    return [line0, col0, col0 + len(token)]


def _occ(rng: list[int], symbol: str, *, definition: bool = False) -> dict[str, Any]:
    """One SCIP occurrence (``symbol_roles`` 1 = Definition, 8 = a read/ref)."""
    return {"range": rng, "symbol": symbol, "symbol_roles": 1 if definition else 8}


def _occ_for(pkg: str, ver: str = "1.0"):
    """Return an occurrence builder that prepends ``scip-python python <pkg> <ver>``."""

    def occ(rng: list[int], descriptor: str, *, define: bool = False) -> dict[str, Any]:
        return _occ(rng, f"scip-python python {pkg} {ver} {descriptor}", definition=define)

    return occ


def _doc(
    rel: str, occs: list[dict[str, Any]], symbols: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A SCIP document; ``symbols`` carries inheritance relationships."""
    doc: dict[str, Any] = {"relative_path": rel, "occurrences": occs}
    if symbols is not None:
        doc["symbols"] = symbols
    return doc


def _index(docs: list[dict[str, Any]]) -> dict[str, Any]:
    """A SCIP index document (UTF-8 encoding → SCIP col == byte offset)."""
    return {"metadata": {"text_document_encoding": 1}, "documents": docs}


def _impl(symbol: str, parent: str) -> dict[str, Any]:
    """A SymbolInformation carrying one ``is_implementation`` relationship."""
    return {
        "symbol": symbol,
        "relationships": [{"symbol": parent, "is_implementation": True}],
    }


class _FakeProducer:
    """Injected producer returning canned indexes by ``--project-name``."""

    def __init__(self, by_name: dict[str, dict[str, Any]]) -> None:
        self.by_name = by_name
        self.calls: list[tuple[Path, str, list[Path]]] = []

    def produce(
        self, project_root: Path, project_name: str, extra_paths: Any
    ) -> dict[str, Any] | None:
        self.calls.append((Path(project_root), project_name, [Path(p) for p in extra_paths]))
        return self.by_name.get(project_name)


# ── Fixture helpers ─────────────────────────────────────────────────────────


def _nodes_from_source(rel: str, text: str) -> list[GraphNode]:
    """Deterministic File/Class/Method/Function nodes from a source's AST.

    These stand in for the tree-sitter extractor's output (same kinds, names, and
    ``_stable_id``-content-addressed ids over byte-start) but are built from the
    stdlib ``ast`` so the resolver tests are hermetic and order-independent — they
    must not inherit the extractor's own nondeterminism. ``ast`` line/col offsets
    are UTF-8 byte offsets, matching our nodes' byte ranges.
    """
    raw = text.encode("utf-8")
    line_starts = [0]
    pos = raw.find(b"\n")
    while pos != -1:
        line_starts.append(pos + 1)
        pos = raw.find(b"\n", pos + 1)

    def span(node: ast.AST) -> tuple[int, int]:
        start = line_starts[node.lineno - 1] + node.col_offset  # type: ignore[attr-defined]
        end = line_starts[node.end_lineno - 1] + node.end_col_offset  # type: ignore[attr-defined]
        return start, end

    def mk(kind: str, name: str, rng: tuple[int, int]) -> GraphNode:
        return make_graph_node(
            slug=SLUG,
            node_id=_stable_id(SLUG, kind, name, rel, rng[0]),
            type=kind,  # type: ignore[arg-type]
            name=name,
            file=rel,
            range=rng,
            docstring=None,
        )

    nodes = [mk("File", rel, (0, len(raw)))]
    for top in ast.parse(text).body:
        if isinstance(top, ast.ClassDef):
            nodes.append(mk("Class", top.name, span(top)))
            for sub in top.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    nodes.append(mk("Method", sub.name, span(sub)))
        elif isinstance(top, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nodes.append(mk("Function", top.name, span(top)))
    return nodes


def _write(root: Path, files: dict[str, str]) -> list[GraphNode]:
    """Write *files*, drop a ``[project]`` ``pyproject.toml``, return fixture nodes."""
    nodes: list[GraphNode] = []
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        nodes.extend(_nodes_from_source(rel, text))
    (root / "pyproject.toml").write_text('[project]\nname = "fixture"\n', encoding="utf-8")
    return nodes


def _one(nodes: list[GraphNode], type_: str, name: str, file: str | None = None) -> GraphNode:
    """The single node matching ``(type, name[, file])`` — asserts uniqueness."""
    cands = [
        n
        for n in nodes
        if n.type == type_ and n.name == name and (file is None or n.file == file)
    ]
    assert len(cands) == 1, f"want 1 {type_} {name!r}, got {len(cands)}"
    return cands[0]


def _named_by_position(
    nodes: list[GraphNode], type_: str, name: str, file: str
) -> list[GraphNode]:
    """All ``(type, name, file)`` nodes, ordered by byte start (outer→inner)."""
    return sorted(
        (n for n in nodes if n.type == type_ and n.name == name and n.file == file),
        key=lambda n: n.range[0],
    )


def _edge(res: ResolutionResult, source: str, target: str, type_: str) -> bool:
    return any(
        e.source == source and e.target == target and e.type == type_ for e in res.edges
    )


def _run(root: Path, by_name: dict[str, dict[str, Any]], nodes: list[GraphNode]):
    producer = _FakeProducer(by_name)
    result = ScipPythonResolver(SLUG, producer=producer).resolve(root, nodes)
    return result, producer


# ── Ambiguous same-named method → CORRECT owner (the headline) ──────────────

_FACTORY = """\
class Driver:
    def validate(self):
        return True


class Other:
    def validate(self):
        return False
"""

_USER = """\
from pkg.factory import Driver, Other


def use_active(active):
    active = Driver()
    return active.validate()


def use_other():
    other = Other()
    return other.validate()
"""


def test_ambiguous_method_resolves_each_call_to_its_real_owner(tmp_path: Path) -> None:
    nodes = _write(tmp_path, {"pkg/factory.py": _FACTORY, "pkg/user.py": _USER})
    occ = _occ_for("pkg")
    fac, usr = "`pkg.factory`", "`pkg.user`"
    factory_doc = _doc(
        "pkg/factory.py",
        [
            occ([0, 0, 0], f"{fac}/__init__:", define=True),
            occ(_loc(_FACTORY, "Driver", 0), f"{fac}/Driver#", define=True),
            occ(_loc(_FACTORY, "validate", 0), f"{fac}/Driver#validate().", define=True),
            occ(_loc(_FACTORY, "Other", 0), f"{fac}/Other#", define=True),
            occ(_loc(_FACTORY, "validate", 1), f"{fac}/Other#validate().", define=True),
        ],
    )
    user_doc = _doc(
        "pkg/user.py",
        [
            occ([0, 0, 0], f"{usr}/__init__:", define=True),
            occ(_loc(_USER, "use_active", 0), f"{usr}/use_active().", define=True),
            occ(_loc(_USER, "use_other", 0), f"{usr}/use_other().", define=True),
            # references
            occ(_loc(_USER, "Driver", 0), f"{fac}/Driver#"),
            occ(_loc(_USER, "Other", 0), f"{fac}/Other#"),
            occ(_loc(_USER, "Driver", 1), f"{fac}/Driver#"),
            occ(_loc(_USER, "validate", 0), f"{fac}/Driver#validate()."),
            occ(_loc(_USER, "Other", 1), f"{fac}/Other#"),
            occ(_loc(_USER, "validate", 1), f"{fac}/Other#validate()."),
        ],
    )
    index = _index([factory_doc, user_doc])
    result, _ = _run(tmp_path, {tmp_path.name: index}, nodes)

    driver_v, other_v = _named_by_position(nodes, "Method", "validate", "pkg/factory.py")
    assert driver_v.node_id != other_v.node_id
    use_active = _one(nodes, "Function", "use_active")
    use_other = _one(nodes, "Function", "use_other")

    # Each call resolves to its OWN class's validate — never collapsed by name.
    assert _edge(result, use_active.node_id, driver_v.node_id, "REFERENCES")
    assert _edge(result, use_other.node_id, other_v.node_id, "REFERENCES")
    assert not _edge(result, use_active.node_id, other_v.node_id, "REFERENCES")
    assert not _edge(result, use_other.node_id, driver_v.node_id, "REFERENCES")
    # Cross-file class references connect file clusters too.
    user_file = _one(nodes, "File", "pkg/user.py")
    driver_cls = _one(nodes, "Class", "Driver")
    assert _edge(result, user_file.node_id, driver_cls.node_id, "REFERENCES")
    assert result.stats.resolved_exact >= 4


# ── EXTENDS (in-repo + external) + External convergence ─────────────────────

_MODELS = """\
import pydantic


class Base:
    pass


class Item(Base):
    pass


class Thing(pydantic.BaseModel):
    pass
"""


def test_extends_and_external_convergence(tmp_path: Path) -> None:
    nodes = _write(tmp_path, {"models.py": _MODELS})
    occ = _occ_for("app")
    m = "`models`"
    pyd = "scip-python python pydantic 2.0 `pydantic.main`/BaseModel#"
    item_sym = f"scip-python python app 1.0 {m}/Item#"
    thing_sym = f"scip-python python app 1.0 {m}/Thing#"
    base_sym = f"scip-python python app 1.0 {m}/Base#"
    doc = _doc(
        "models.py",
        [
            occ([0, 0, 0], f"{m}/__init__:", define=True),
            occ(_loc(_MODELS, "Base", 0), f"{m}/Base#", define=True),
            occ(_loc(_MODELS, "Item", 0), f"{m}/Item#", define=True),
            occ(_loc(_MODELS, "Thing", 0), f"{m}/Thing#", define=True),
            # `class Item(Base)` refs Base; `Thing(pydantic.BaseModel)` refs external
            occ(_loc(_MODELS, "Base", 1), f"{m}/Base#"),
            _occ(_loc(_MODELS, "BaseModel", 0), pyd),
        ],
        symbols=[_impl(item_sym, base_sym), _impl(thing_sym, pyd)],
    )
    result, _ = _run(tmp_path, {tmp_path.name: _index([doc])}, nodes)

    base = _one(nodes, "Class", "Base")
    item = _one(nodes, "Class", "Item")
    thing = _one(nodes, "Class", "Thing")

    # in-repo inheritance → EXTENDS to the REAL parent node
    assert _edge(result, item.node_id, base.node_id, "EXTENDS")
    # out-of-repo inheritance → EXTENDS to a synthesized External node
    assert len(result.externals) == 1
    ext = result.externals[0]
    assert ext.type == "External"
    assert ext.name == "pydantic.main.BaseModel"
    assert _edge(result, thing.node_id, ext.node_id, "EXTENDS")
    # the superclass reference + the EXTENDS relationship converge on ONE node
    assert _edge(result, thing.node_id, ext.node_id, "REFERENCES")
    assert result.stats.extends == 2


_OVERRIDE = """\
class Base:
    def run(self):
        pass


class Item(Base):
    def run(self):
        pass
"""


def test_method_level_implementation_is_not_extends(tmp_path: Path) -> None:
    """A METHOD overriding a base method must NOT become an EXTENDS edge.

    pyright/scip-python sets ``is_implementation`` on a method override too —
    not just class inheritance. ``_resolve_relationships`` used to emit an
    EXTENDS edge for ANY ``is_implementation`` relationship regardless of the
    subject's kind, sourcing it at the Method node. `CodeGraph`'s endpoint
    rule (EXTENDS sources are Class/Interface/Object only) then rejected the
    WHOLE graph at ingest for any repo with a single overridden method — this
    guards the fix (scope to TYPE-kind subjects only).
    """
    nodes = _write(tmp_path, {"models.py": _OVERRIDE})
    occ = _occ_for("app")
    m = "`models`"
    base_run_desc, item_run_desc = f"{m}/Base#run().", f"{m}/Item#run()."
    base_run_sym = f"scip-python python app 1.0 {base_run_desc}"
    item_run_sym = f"scip-python python app 1.0 {item_run_desc}"
    doc = _doc(
        "models.py",
        [
            occ([0, 0, 0], f"{m}/__init__:", define=True),
            occ(_loc(_OVERRIDE, "Base", 0), f"{m}/Base#", define=True),
            occ(_loc(_OVERRIDE, "run", 0), base_run_desc, define=True),
            occ(_loc(_OVERRIDE, "Item", 0), f"{m}/Item#", define=True),
            occ(_loc(_OVERRIDE, "run", 1), item_run_desc, define=True),
        ],
        # Item.run() "implements" Base.run() — a METHOD-level is_implementation
        # relationship, matching real pyright/scip-python output for overrides.
        symbols=[_impl(item_run_sym, base_run_sym)],
    )
    result, _ = _run(tmp_path, {tmp_path.name: _index([doc])}, nodes)

    assert not any(e.type == "EXTENDS" for e in result.edges)
    assert result.stats.extends == 0


# ── Cross-project descriptor stitching (full symbol misses on pkg/ver) ──────


def test_cross_project_descriptor_stitch(tmp_path: Path) -> None:
    lib_core = "class Helper:\n    def run(self):\n        return 1\n"
    app_main = "from lib.core import Helper\n\n\ndef use():\n    return Helper().run()\n"
    nodes = _write(
        tmp_path,
        {"lib/core.py": lib_core, "app/main.py": app_main},
    )
    # two roots, no pyproject at repo root — discovery finds app + lib
    (tmp_path / "lib" / "pyproject.toml").write_text('[project]\nname="lib"\n')
    (tmp_path / "app" / "pyproject.toml").write_text('[project]\nname="app"\n')

    lib = _occ_for("lib", ver="9.9.9")  # lib's own index → correct version
    lib_index = _index(
        [
            _doc(
                "core.py",
                [
                    lib([0, 0, 0], "`lib.core`/__init__:", define=True),
                    lib(_loc(lib_core, "Helper", 0), "`lib.core`/Helper#", define=True),
                    lib(_loc(lib_core, "run", 0), "`lib.core`/Helper#run().", define=True),
                ],
            )
        ]
    )
    # app's reference carries the WRONG version (0.0.0 — no virtualenv mapping);
    # only the descriptor tail is byte-identical, so it must STITCH.
    app = _occ_for("app")
    ref = _occ_for("lib", ver="0.0.0")  # app's view of lib — version mismatch
    app_index = _index(
        [
            _doc(
                "main.py",
                [
                    app([0, 0, 0], "`app.main`/__init__:", define=True),
                    app(_loc(app_main, "use", 0), "`app.main`/use().", define=True),
                    ref(_loc(app_main, "Helper", 1), "`lib.core`/Helper#"),
                    ref(_loc(app_main, "run", 0), "`lib.core`/Helper#run()."),
                ],
            )
        ]
    )
    result, producer = _run(tmp_path, {"lib": lib_index, "app": app_index}, nodes)

    use = _one(nodes, "Function", "use")
    run = _one(nodes, "Method", "run", "lib/core.py")
    assert _edge(result, use.node_id, run.node_id, "REFERENCES")
    assert result.stats.resolved_stitched >= 1
    assert result.stats.external == 0  # lib is in-repo, NOT external
    # sibling import roots are offered to the producer (pyrightconfig extraPaths)
    app_call = next(c for c in producer.calls if c[1] == "app")
    assert (tmp_path / "lib") in app_call[2]


# ── Ambiguous descriptor across projects → dropped (never mislinks) ─────────


def test_ambiguous_descriptor_is_dropped_not_mislinked(tmp_path: Path) -> None:
    a_src = "class Settings:\n    def load(self):\n        return 1\n"
    b_src = "class Settings:\n    def load(self):\n        return 2\n"
    c_src = "def go():\n    return None\n"
    nodes = _write(
        tmp_path,
        {"a/shared/config.py": a_src, "b/shared/config.py": b_src, "c/main.py": c_src},
    )
    for name in ("a", "b", "c"):
        (tmp_path / name / "pyproject.toml").write_text(f'[project]\nname="{name}"\n')

    def settings_index(pkg: str) -> dict[str, Any]:
        occ = _occ_for(pkg)
        occs = [
            occ([0, 0, 0], "`shared.config`/__init__:", define=True),
            occ(_loc(a_src, "Settings", 0), "`shared.config`/Settings#", define=True),
            occ(_loc(a_src, "load", 0), "`shared.config`/Settings#load().", define=True),
        ]
        return _index([_doc("shared/config.py", occs)])

    cocc = _occ_for("c")
    c_index = _index(
        [
            _doc(
                "main.py",
                [
                    cocc([0, 0, 0], "`c.main`/__init__:", define=True),
                    cocc(_loc(c_src, "go", 0), "`c.main`/go().", define=True),
                    # descriptor matches BOTH a and b → unresolvable, must drop
                    _occ_for("shared", ver="0.0")(
                        [1, 11, 15], "`shared.config`/Settings#load()."
                    ),
                ],
            )
        ]
    )
    result, _ = _run(
        tmp_path,
        {"a": settings_index("a"), "b": settings_index("b"), "c": c_index},
        nodes,
    )

    go = _one(nodes, "Function", "go")
    a_load = _one(nodes, "Method", "load", "a/shared/config.py")
    b_load = _one(nodes, "Method", "load", "b/shared/config.py")
    assert not _edge(result, go.node_id, a_load.node_id, "REFERENCES")
    assert not _edge(result, go.node_id, b_load.node_id, "REFERENCES")
    assert result.stats.dropped_ambiguous >= 1


# ── In-repo-but-unmodelled (attribute) → dropped, not faked External ────────

_ATTR = """\
class C:
    def __init__(self):
        self.status = 1

    def read(self):
        return self.status
"""


def test_in_repo_attribute_reference_is_dropped(tmp_path: Path) -> None:
    nodes = _write(tmp_path, {"m.py": _ATTR})
    occ = _occ_for("app")
    m = "`m`"
    doc = _doc(
        "m.py",
        [
            occ([0, 0, 0], f"{m}/__init__:", define=True),
            occ(_loc(_ATTR, "C", 0), f"{m}/C#", define=True),
            occ(_loc(_ATTR, "__init__", 0), f"{m}/C#__init__().", define=True),
            occ(_loc(_ATTR, "read", 0), f"{m}/C#read().", define=True),
            occ(_loc(_ATTR, "status", 0), f"{m}/C#status.", define=True),
            # `return self.status` references an in-repo ATTRIBUTE we don't model
            occ(_loc(_ATTR, "status", 1), f"{m}/C#status."),
        ],
    )
    result, _ = _run(tmp_path, {tmp_path.name: _index([doc])}, nodes)

    read = _one(nodes, "Method", "read")
    # the only reference inside read() is the unmodelled attribute → no edge,
    # and crucially NOT synthesized as an External (it is in-repo)
    assert not any(e.source == read.node_id for e in result.edges)
    assert result.externals == []
    assert result.stats.dropped_unmodelled >= 1


# ── Graceful degradation ────────────────────────────────────────────────────


def test_unavailable_producer_returns_empty_result(tmp_path: Path) -> None:
    nodes = _write(tmp_path, {"m.py": "def f():\n    return 1\n"})
    # producer returns None for every root → nothing indexed
    result, _ = _run(tmp_path, {}, nodes)
    assert result.edges == []
    assert result.externals == []
    assert result.available is False


def test_discover_roots_finds_project_tables_only(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pyproject.toml").write_text('[project]\nname="a"\n')
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "pyproject.toml").write_text("[tool.black]\nline-length = 88\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "pyproject.toml").write_text('[project]\nname="vendored"\n')

    roots = ScipPythonResolver.discover_roots(tmp_path)
    assert tmp_path / "a" in roots  # has [project]
    assert tmp_path / "b" not in roots  # only [tool.black]
    assert tmp_path / ".venv" / "lib" not in roots  # pruned vendored dir


def test_repo_prefix_and_join_map_scip_paths_to_repo_relative(tmp_path: Path) -> None:
    # The SCIP-path → GraphNode.file binding contract: prefix = project root
    # relative to the repo root, joined with the doc's project-relative path.
    repo = tmp_path.resolve()
    prefix = ScipPythonResolver._repo_prefix
    join = ScipPythonResolver._join

    assert prefix(repo, repo) == ""  # project root == repo root → no prefix
    assert prefix(repo, repo / "projects" / "demoapp") == "projects/demoapp"
    assert prefix(repo, Path("/elsewhere/pkg")) == ""  # outside repo → no prefix

    # join produces the repo-relative path that equals GraphNode.file.
    assert join("projects/demoapp", "pkg/x.py") == "projects/demoapp/pkg/x.py"
    assert join("", "main.py") == "main.py"  # top-level project root
    assert join("lib/core", "") == "lib/core"  # module-self / dir doc


# ── Integration: real scip-python + scip (skipped when absent) ──────────────


@pytest.mark.skipif(
    not (shutil.which("scip-python") and shutil.which("scip")),
    reason="scip-python and/or scip binaries not installed",
)
def test_real_scip_python_resolves_cross_file_call(tmp_path: Path) -> None:
    b_src = (
        "from pkg.a import Widget\n\n\n"
        "def build():\n    w = Widget()\n    return w.render()\n"
    )
    files = {
        "pkg/__init__.py": "",
        "pkg/a.py": "class Widget:\n    def render(self):\n        return 'w'\n",
        "pkg/b.py": b_src,
    }
    nodes = _write(tmp_path, files)
    result = ScipPythonResolver(
        SLUG, producer=SubprocessScipProducer()
    ).resolve(tmp_path, nodes)

    assert result.available
    build = _one(nodes, "Function", "build")
    render = _one(nodes, "Method", "render", "pkg/a.py")
    widget = _one(nodes, "Class", "Widget")
    assert _edge(result, build.node_id, render.node_id, "REFERENCES")
    # the cross-file class reference resolves to the real Widget node, not a guess
    assert any(
        e.target == widget.node_id and e.type == "REFERENCES" for e in result.edges
    )
    assert result.stats.resolved_exact >= 2
