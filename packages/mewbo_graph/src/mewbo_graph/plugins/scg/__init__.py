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

**General availability (#83-B).** The ``scg`` capability is no longer granted
ONLY by an orchestrated/workspace-bound run advertising it. Importing this
package registers a runtime *capability provider* with core
(:func:`register_scg_capability`) so any ORDINARY session (CLI chat, console
task, channel) gets the ``scg`` capability — and therefore the
``scg_route`` / ``scg_observe`` / ``scg_memory`` reasoning tools — whenever the
runtime predicate holds: ``scg.enabled`` is on AND the SCG store has at least
one mapped source. An unscoped session binds no :class:`ScgScope`, so its reads
see the WHOLE graph (the scope default); a workspace-bound run keeps today's
source-scoped behaviour. The grant flows through the unchanged
``requires-capabilities`` gate — no tool-filter hack.
"""

from __future__ import annotations


def _scg_runtime_capability(advertised: tuple[str, ...]) -> tuple[str, ...]:
    """Grant ``scg`` to any session once the SCG is usable (the #83-B predicate).

    Returns ``("scg",)`` when the feature is enabled (``scg.enabled``) AND the
    structure store holds at least one mapped source; otherwise ``()``. A no-op
    when ``scg`` is already advertised (a workspace-bound run) so the provider
    never double-grants. Best-effort + import-guarded: a core-only install (the
    optional SCG engine absent) or an unreachable store yields ``()`` rather than
    raising — core's ``augment_session_capabilities`` also nets any escape, but
    failing closed here keeps a lean install silent. The predicate is evaluated
    PER session-init, so mapping the first source flips a live process with no
    restart (mirrors ``get_search_runner``'s per-run resolution).
    """
    if "scg" in advertised:
        return ()
    try:
        from mewbo_core.config import get_config_value

        if not bool(get_config_value("scg", "enabled")):
            return ()
        from mewbo_graph.scg.store import get_scg_store

        if get_scg_store().list_sources():
            return ("scg",)
    except Exception:  # noqa: BLE001 — a predicate must never break session init
        return ()
    return ()


def register_scg_capability() -> None:
    """Register the SCG runtime capability provider with core (down-only push).

    Idempotent (core dedupes on identity). Called once on package import so the
    ``scg`` capability surfaces to ordinary sessions wherever the predicate holds
    — without core importing up to evaluate ``scg.enabled`` / the store.
    """
    from mewbo_core.capabilities import register_session_capability_provider

    register_session_capability_provider(_scg_runtime_capability)


# Self-register on import: any host that imports the scg plugin suite (the API
# under the `wiki` extra, or a test importing this package) gets the runtime
# `scg` capability provider wired into core's session-init read-point.
register_scg_capability()
