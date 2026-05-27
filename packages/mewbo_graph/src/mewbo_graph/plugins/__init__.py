"""Bundled plugin suites shipped with the graph substrate.

``wiki`` (indexing + Q&A SessionTools + AgentDefs) and ``scg`` (map + search
SessionTools + AgentDefs). They ship here — with the engine they wrap —
rather than in the core wheel, so they can import the substrate **down**
(``mewbo_graph.{wiki,scg}``) instead of up into an app. Discovery is by
filesystem scan of this directory's ``.claude-plugin/plugin.json`` manifests;
:func:`mewbo_graph.register_builtin_plugins` registers this root with the core
plugin loader on import.
"""
from __future__ import annotations
