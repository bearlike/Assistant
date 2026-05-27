"""Source Capability Graph (SCG) — the reachability engine for Mewbo Search.

Indexes *reachability* (schemas + qualified pathways, **never data**): types,
store, providers, parser, router, entity resolution, memory bridge. The API
layer owns the run/map-job lifecycle + transport; the ``scg`` SessionTools
(``mewbo_graph.plugins.scg``) drive these deterministic ops. ``ScgConfig``
(the ``scg.enabled`` gate) stays in the API as glue — it is read only by api
modules, never by this engine.
"""
from __future__ import annotations
