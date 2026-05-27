"""Built-in ``scg`` plugin — Source Capability Graph map + search tools.

The SCG indexes *reachability* (schemas + qualified pathways, **never the data
behind them**) so Agentic Search can route a query to executable connector
pathways and deploy sub-agents along them. This plugin exposes the deterministic
SCG core (which lives **down** in the same library at ``mewbo_graph.scg``) as
SessionTools, plus the three AgentDefs that drive map (indexing) and search
(traversal). Tools and agents register via the manifest at
``.claude-plugin/plugin.json``.

The whole feature is gated on the ``scg`` capability: an Agentic Search map/run
session advertises ``client_capabilities: ["scg"]`` so the AgentDefs surface in
``spawn_agent`` lookups — mirroring the wiki plugin's ``wiki`` capability gate.
The deterministic core is opt-in via the optional ``mewbo-graph`` extras and the
``scg.enabled`` config flag; if those extras are absent, every tool degrades to
a structured error rather than crashing the host.
"""
