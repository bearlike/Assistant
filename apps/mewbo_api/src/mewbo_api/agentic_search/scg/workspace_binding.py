"""WorkspaceGraphBinding — the ONE seam that turns a workspace into graph access.

#77 widens the gate the ``OrchestratedSearchRunner`` used to own alone: *any*
run type that binds a workspace gets the ``scg`` capability + the graph
traversal tools (``scg_route`` / ``scg_memory`` / fan-out verbs) + the workspace
source scope. Before this seam each of those three facts was assembled inline in
the search runner, so the structured graph-first path (and any future binding)
would have had to copy them. This atomic class is the single resolution point:

    binding = WorkspaceGraphBinding.for_workspace(workspace, allowed_tools, project)
    for ctx in binding.context_events:        # capability + quarantined instructions
        runtime.append_context_event(sid, ctx)
    with binding.scope():                      # ScgScope bound for the worker thread
        runtime.run_sync(..., allowed_tools=binding.allowed_tools(), ...)

Three resolved facts, one place:

* **capability context events** — ``client_capabilities: ["scg"]`` (so the
  ``scg-*`` AgentDefs + ``scg_*`` tools gate in, mirroring wiki jobs) plus the
  workspace's UNTRUSTED ``instructions`` as an explicitly-labelled context event
  (NEVER the system prompt — the security invariant the runner already upheld);
* **allowed_tools** — the run's scoped connector grant (sources ∩ ``filter_specs``,
  already resolved upstream by ``SourceCatalog.tools_for`` via the #75 virtual
  config) UNIONed with the fixed SCG traversal verbs, de-duplicated;
* **scope** — the workspace source allowlist bound on ``ScgScope`` (#75) so the
  un-owned ``scg_route`` plugin tool only ranks pathways through the workspace's
  own sources. Import-guarded: an absent ``mewbo-graph`` SCG engine degrades to an
  unscoped (no-op) bind rather than crashing the drive.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from ..schemas import Workspace

# Traversal verbs the graph-driving agent always needs, independent of which
# connector tools a run's sources unlock. Unioned with the run's scoped grant.
# ``scg_observe`` (Search-on-Graph navigation) is granted here so it is available
# once it lands; ``filter_specs`` silently drops it until then (graceful).
TRAVERSAL_TOOLS: tuple[str, ...] = (
    "scg_route",
    "scg_observe",
    "scg_memory",
    "spawn_agent",
    "check_agents",
    "steer_agent",
)

# The capability a graph-bound session advertises so ``spawn_agent`` can look up
# the scg-search / scg-path-probe AgentDefs and the ``scg_*`` tools scope in
# (gating mirrors wiki jobs.py — see plugins/scg/CLAUDE.md "Capability gating").
SCG_CAPABILITY = "scg"


@dataclass(frozen=True)
class WorkspaceGraphBinding:
    """Resolved graph-access facts for a workspace-bound run (one atomic unit).

    Built by :meth:`for_workspace`; carries the source allowlist + the run's
    scoped connector grant as state and exposes the three derived facts as
    behaviors. Holds no runtime — a caller threads the context events onto its
    session, scopes ``run_sync`` with :meth:`scope`, and grants
    :meth:`allowed_tools`. Reused by both the search drive and the structured
    graph-first drive so the gate widens in exactly one place.
    """

    source_ids: list[str]
    connector_grant: list[str]
    instructions: str | None = None
    extra_capabilities: tuple[str, ...] = field(default_factory=tuple)
    workspace_id: str | None = None

    @classmethod
    def for_workspace(
        cls,
        workspace: Workspace,
        connector_grant: list[str],
        *,
        extra_capabilities: tuple[str, ...] = (),
    ) -> WorkspaceGraphBinding:
        """Resolve the binding from *workspace* + its already-scoped tool grant.

        ``connector_grant`` is the run's path-capability grant
        (``RunRecord.allowed_tools`` for a search run, or
        ``SourceCatalog.tools_for`` for a structured run) — sources ∩
        ``filter_specs``, NEVER the full catalog. The traversal verbs are
        appended by :meth:`allowed_tools`; only the connector grant is stored.
        ``extra_capabilities`` lets a caller advertise an additional capability
        (e.g. ``wiki`` for a structured run that may also touch wiki grounding)
        alongside ``scg``.
        """
        return cls(
            source_ids=list(workspace.sources),
            connector_grant=list(connector_grant),
            instructions=workspace.instructions or None,
            extra_capabilities=tuple(extra_capabilities),
            workspace_id=workspace.id or None,
        )

    @property
    def capabilities(self) -> list[str]:
        """The capabilities this binding advertises (``scg`` + any extras)."""
        out = [SCG_CAPABILITY]
        for cap in self.extra_capabilities:
            if cap not in out:
                out.append(cap)
        return out

    @property
    def context_events(self) -> list[dict[str, object]]:
        """The context events a caller must append to gate graph access in.

        Two writes, in order: the capability advertisement (so the AgentDefs +
        ``scg_*`` tools surface) and — only when the workspace carries
        ``instructions`` — the UNTRUSTED text as an explicitly-labelled event.
        The label is the quarantine: the agent may consult it via tools, but it
        is NEVER concatenated into a system/developer prompt (the security
        invariant; see scg/CLAUDE.md "Security invariants").
        """
        events: list[dict[str, object]] = [{"client_capabilities": self.capabilities}]
        if self.instructions:
            events.append({"untrusted_workspace_instructions": self.instructions})
        return events

    def allowed_tools(self) -> list[str]:
        """Union the scoped connector grant with the fixed SCG traversal verbs.

        De-duplicated, selection order preserved (connector grant first, then the
        traversal verbs) so the graph-driving agent can route + fan out while
        staying bounded to the workspace's own connector surface.
        """
        seen: set[str] = set()
        out: list[str] = []
        for tool_id in (*self.connector_grant, *TRAVERSAL_TOOLS):
            if tool_id not in seen:
                seen.add(tool_id)
                out.append(tool_id)
        return out

    @contextmanager
    def scope(self) -> Iterator[None]:
        """Bind the workspace SCG source scope (#75) for the wrapped block.

        Delegates to :class:`mewbo_graph.scg.scope.ScgScope` so the un-owned
        ``scg_route`` plugin tool transparently routes only within the
        workspace's sources. ``workspace=`` carries the workspace id for #76
        deposit ATTRIBUTION (which workspace LEARNED a fact — never a partition);
        without it a connector insight is deposited ``workspace=None`` (graceful
        but dormant). Import-guarded: a core-only install (the ``mewbo-graph`` SCG
        engine absent) degrades to an unscoped (no-op) bind rather than crashing
        the drive — the drive only reaches here when ``scg.enabled`` AND a source
        is mapped, but the guard keeps an absent engine safe.
        """
        try:
            from mewbo_graph.scg.scope import ScgScope
        except ImportError:
            yield
            return
        with ScgScope.use(self.source_ids, workspace=self.workspace_id):
            yield


__all__ = ["WorkspaceGraphBinding", "TRAVERSAL_TOOLS", "SCG_CAPABILITY"]
