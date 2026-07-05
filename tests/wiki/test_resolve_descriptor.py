"""SCIP descriptor parsing — locked against REAL symbol strings from spike.json.

The descriptor tail is the cross-project stitching key, so its parse must be
byte-faithful. Every literal below was captured from a real scip-python index of
a large internal Python monorepo (the spike's ``spike.json``); the leaf
classification drives which node kind a definition can map to.
"""
from __future__ import annotations

import pytest
from mewbo_graph.wiki.resolve.descriptor import LeafKind, ScipSymbol


@pytest.mark.parametrize(
    ("raw", "descriptor", "leaf_name", "leaf_kind"),
    [
        # class (type) leaf
        (
            "scip-python python demoapp 0.2.0 "
            "`pkg.examples`/Widget#",
            "`pkg.examples`/Widget#",
            "Widget",
            LeafKind.TYPE,
        ),
        # method leaf (has a class owner via '#')
        (
            "scip-python python demoapp 0.2.0 "
            "`pkg.logger`/Service#__init__().",
            "`pkg.logger`/Service#__init__().",
            "__init__",
            LeafKind.CALLABLE,
        ),
        # module self-symbol → meta leaf → File node
        (
            "scip-python python demoapp 0.2.0 `pkg.logger`/__init__:",
            "`pkg.logger`/__init__:",
            "__init__",
            LeafKind.MODULE,
        ),
        # bare module path (no backticks) → meta leaf
        (
            "scip-python python demoapp 0.2.0 pkg/__init__:",
            "pkg/__init__:",
            "__init__",
            LeafKind.MODULE,
        ),
        # attribute (term) leaf → no node
        (
            "scip-python python demoapp 0.2.0 `pkg.logger`/console.",
            "`pkg.logger`/console.",
            "console",
            LeafKind.OTHER,
        ),
        # parameter leaf ((name) form) → no node
        (
            "scip-python python demoapp 0.2.0 "
            "`pkg.logger`/Service#__init__().(self)",
            "`pkg.logger`/Service#__init__().(self)",
            "self",
            LeafKind.OTHER,
        ),
        # nested function (method-under-method) → callable leaf, but in practice
        # has no tree-sitter node so it never maps; still parses cleanly.
        (
            "scip-python python demoapp 0.2.0 "
            "`pkg.integration.test_worker`/test_retry()._run_once().",
            "`pkg.integration.test_worker`/test_retry()._run_once().",
            "_run_once",
            LeafKind.CALLABLE,
        ),
        # external package symbol stitches by descriptor (pkg/ver differ, tail same)
        (
            "scip-python python pydantic 2.12.5 `pydantic.main`/BaseModel#",
            "`pydantic.main`/BaseModel#",
            "BaseModel",
            LeafKind.TYPE,
        ),
    ],
)
def test_parses_real_scip_symbols(
    raw: str, descriptor: str, leaf_name: str, leaf_kind: LeafKind
) -> None:
    sym = ScipSymbol.parse(raw)
    assert not sym.is_local
    assert sym.descriptor == descriptor
    assert sym.leaf_name == leaf_name
    assert sym.leaf_kind is leaf_kind


def test_descriptor_is_identical_across_pkg_ver() -> None:
    # The stitching invariant: same descriptor tail, different <pkg> <ver>.
    a = ScipSymbol.parse("scip-python python demoapp 0.0.0 `demoapp.llm`/Client#run().")
    b = ScipSymbol.parse("scip-python python demoapp 9.9.9 `demoapp.llm`/Client#run().")
    assert a.descriptor == b.descriptor
    assert a.descriptor == "`demoapp.llm`/Client#run()."


def test_local_symbol_is_marked_and_unlinkable() -> None:
    sym = ScipSymbol.parse("local 42")
    assert sym.is_local
    assert sym.descriptor == ""
    assert sym.leaf_name is None
    assert sym.leaf_kind is LeafKind.OTHER


def test_readable_joins_component_names() -> None:
    sym = ScipSymbol.parse("scip-python python rich 14.3.3 `rich.console`/Console#print().")
    assert sym.readable == "rich.console.Console.print"


def test_non_scip_string_is_returned_verbatim_descriptor() -> None:
    # Defensive: an unexpected prefix is not split; never raises.
    sym = ScipSymbol.parse("something else entirely")
    assert sym.descriptor == "something else entirely"
