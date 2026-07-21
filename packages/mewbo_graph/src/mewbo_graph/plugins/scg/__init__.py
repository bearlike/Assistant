"""Built-in ``scg`` plugin — Source Capability Graph map + search tools.

The SCG indexes *reachability* (schemas + qualified pathways, **never the data
behind them**) so Agentic Search can route a query to executable connector
pathways and deploy sub-agents along them. This plugin exposes the deterministic
SCG core (which lives **down** in the same library at ``mewbo_graph.scg``) as
SessionTools, plus the AgentDefs that drive map (indexing) and search
(traversal). Tools and agents register via the manifest at
``.claude-plugin/plugin.json``.

The whole feature is gated on the ``scg`` capability: a session reaches the
``scg_*`` / ``agentic_search`` tools and the map/search AgentDefs ONLY when it
EXPLICITLY advertises ``client_capabilities: ["scg"]`` — every Agentic Search
map/run/structured surface does (see ``agentic_search/scg/workspace_binding`` +
``map_job``) — or names one of the tools in its per-request ``allowed_tools``
(request-scoped capability derivation). The deterministic core is
additionally opt-in behind the optional ``mewbo-graph`` extras and the
``scg.enabled`` config flag; absent those, every tool degrades to a structured
error rather than crashing the host.

**No blanket runtime grant.** An earlier iteration registered a
runtime *capability provider* that granted ``scg`` to ANY session once
``scg.enabled`` was on AND one source was mapped — including a bare
``POST /api/sessions`` coding session that requested no integrations. That bound
all 12 scg/agentic_search tool schemas on every LLM call (~half the catalog) for
tools the session never used, and misclassified its provenance as
``origin:search`` (the augmented capability set leaks into the trace context that
:class:`SessionOrigin` reads). The grant is now advertisement-only: the provider
is removed, so a session that never asked for search carries a lean toolset and
neutral provenance. Core's generic ``register_session_capability_provider`` seam
remains for any future capability, but this suite registers nothing into it.
"""

from __future__ import annotations
