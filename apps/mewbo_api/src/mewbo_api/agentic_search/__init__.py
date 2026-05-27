"""Agentic Search REST API — workspaces, runs, and SCG indexing.

See ``routes.py`` for the wire contract and ``store.py`` for the persistence
layer. ``init_agentic_search`` wires the namespace, captures the session
runtime, and — when ``scg.enabled`` is on AND at least one source is already
mapped — swaps the default :class:`~mewbo_api.agentic_search.runner.EchoSearchRunner`
for the real :class:`~mewbo_api.agentic_search.scg.orchestrated_runner.OrchestratedSearchRunner`
(otherwise the echo runner stays the default).
"""

from .routes import agentic_ns, init_agentic_search

__all__ = ["agentic_ns", "init_agentic_search"]
