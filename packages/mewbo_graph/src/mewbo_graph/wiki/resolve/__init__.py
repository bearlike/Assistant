"""Faithful symbol resolution for the wiki code graph.

The tree-sitter extractor (``wiki/graph.py``) emits cross-file edges by *name*,
which mislinks every same-named method/class to one node. This package resolves
those edges EXACTLY instead, using a per-language semantic backend behind one
:class:`SymbolResolver` seam. ``ScipPythonResolver`` is the Python backend
(scip-python / Pyright). It is standalone — the indexer wires it in separately —
and degrades cleanly (an empty, ``available is False`` result) when its external
binaries are absent.
"""
from __future__ import annotations

from .base import ResolutionResult, ResolutionStats, SymbolResolver
from .descriptor import LeafKind, ScipSymbol
from .scip_python import (
    ScipIndexProducer,
    ScipPythonResolver,
    SubprocessScipProducer,
)

__all__ = [
    "LeafKind",
    "ResolutionResult",
    "ResolutionStats",
    "ScipIndexProducer",
    "ScipPythonResolver",
    "ScipSymbol",
    "SubprocessScipProducer",
    "SymbolResolver",
]
