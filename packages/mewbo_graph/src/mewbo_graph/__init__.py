"""Mewbo Graph — the optional knowledge-graph substrate.

A single shared engine powering both **MewboWiki** (code wiki indexing/Q&A)
and **Mewbo Search** (the Source Capability Graph): tree-sitter code graph,
the multiplex atomic-note memory engine, the embedder, the hybrid retriever,
and the SCG reachability router. It sits one layer above ``mewbo-core`` and
imports strictly **down** — never an app, never ``mewbo-tools``.

Optional and dependency-ignorable: heavy deps live behind the ``treesitter``
and ``retrieval`` extras, and every import site is guarded so the feature is
simply absent (never a crash) when the extra is uninstalled.

Importing this package registers its bundled plugin suites (``wiki`` and
``scg``) with the core plugin loader, so a host that depends on
``mewbo-graph`` (e.g. ``mewbo-api[wiki]``) gets them discovered automatically
— see :func:`register_builtin_plugins`.
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["plugins_root", "register_builtin_plugins", "register_runtime_capabilities"]


def plugins_root() -> Path:
    """Return the directory holding this package's bundled plugin suites."""
    return Path(__file__).resolve().parent / "plugins"


def register_builtin_plugins() -> None:
    """Register the bundled plugin root with the core plugin loader.

    Idempotent and down-only (graph → core). Called once on import so any
    host that imports ``mewbo_graph`` (the API under the ``wiki`` extra) gets
    the ``wiki`` / ``scg`` plugin suites discovered without the core wheel
    ever importing up into this package.
    """
    from mewbo_core.plugins import register_builtin_root

    register_builtin_root(plugins_root())


def register_runtime_capabilities() -> None:
    """Register the library's runtime capability providers with core (#83-B).

    Idempotent and down-only. Currently the ``scg`` provider: it surfaces the
    ``scg`` capability (and so the ``scg_route`` / ``scg_observe`` / ``scg_memory``
    reasoning tools) to ORDINARY sessions whenever ``scg.enabled`` is on AND a
    source is mapped — without the client advertising it. The provider callable
    only late-imports the SCG store INSIDE its body, so wiring it here pulls in
    nothing heavy at import time and a core-only install stays safe.
    """
    from mewbo_graph.plugins.scg import register_scg_capability

    register_scg_capability()


# Self-register on import: any host that imports `mewbo_graph` (the API under
# the `wiki` extra, or a test importing a submodule) gets the wiki/scg plugin
# suites discovered AND the runtime capability providers wired into core's
# session-init read-point — without core ever importing up to find them.
# Idempotent.
register_builtin_plugins()
register_runtime_capabilities()
