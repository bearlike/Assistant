"""GraphStructuredRunner — route ``/v1/structured`` graph-first over the SCG.

#77 centrepiece: a structured run that binds a *search workspace* should go
**graph-first** — route → spawn a probe per pathway → aggregate → emit — instead
of the wiki-grounded single-agent default. Per ``docs/features-structured-outputs.md``
the run stays an ORDINARY agentic session (the same ``StructuredResponder`` /
``ToolUseLoop`` — NOT a separate execution path); the graph-first discipline is a
schema constraint + a capability grant + a playbook layered on top.

This atomic class is the thin app-side composition seam. Given a resolved
:class:`Workspace` it:

* builds the ONE workspace-binding seam (:class:`WorkspaceGraphBinding`) — the
  ``scg`` capability advertisement, the connector grant ∪ traversal verbs, the
  quarantined untrusted instructions, and the ``ScgScope`` source scope;
* injects them into a :class:`~mewbo_core.structured_response.StructuredResponder`
  via its additive graph-first seam (``capabilities`` / ``context_events`` /
  ``extra_instructions`` / ``scope_factory``) plus the ``scg-search-structured``
  playbook so the terminal is the schema-validated ``emit_result``;
* starts the run async on the same storeless ``"<session_id>:r1"`` handle the
  wiki path uses — so the wire shape (``{run_id, status}`` →
  ``GET /v1/structured/<run_id>``) is unchanged.

Streaming is automatic and uses the SAME mechanism as everything else: the
backing session publishes its ``sub_agent`` probe fan-out to the core
``SessionEventBus`` (the SideStage seam), which the console's session SSE stream
tails live — no run-store projection needed (a structured run is read via the
session transcript, not the search run event log).

The deterministic SCG engine + the gating stay where they are; this only widens
the GRANT to a structured run, per plugins/scg/CLAUDE.md "Capability gating".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mewbo_core.common import get_logger
from mewbo_core.structured_response import StructuredResponder

from ..catalog import SourceCatalog
from ..mcp_config import WorkspaceMcpConfig
from ..schemas import Workspace
from ..store import AgenticSearchStoreBase
from .config import ScgConfig
from .playbooks import load_playbook
from .workspace_binding import WorkspaceGraphBinding

logging = get_logger(name="api.agentic_search.scg.graph_structured_runner")

# The graph-first structured playbook (the trusted skill_instructions extension).
_STRUCTURED_PLAYBOOK = "scg-search-structured"


@dataclass(frozen=True)
class GraphStructuredRunner:
    """Drive a graph-first structured run over a search workspace (one atomic unit).

    Holds the store + the active SCG predicate as state; resolves a workspace,
    builds the binding, and composes a :class:`StructuredResponder`. Stateless
    per run otherwise — the session transcript IS the run record, the same as the
    wiki structured path.
    """

    store: AgenticSearchStoreBase

    def workspace_for(self, ref: str) -> Workspace | None:
        """Resolve *ref* (workspace id OR case-insensitive name) to a workspace.

        Returns ``None`` when no workspace matches — the caller then treats
        ``ref`` as a wiki slug and falls back to the default grounding path, so a
        non-search ``workspace`` value never breaks ``/v1/structured``.
        """
        workspace = self.store.get_workspace(ref)
        if workspace is not None:
            return workspace
        matches = [
            w
            for w in self.store.list_workspaces()
            if str(w.name).lower() == ref.lower()
        ]
        return matches[0] if len(matches) == 1 else None

    def is_graph_eligible(self, workspace: Workspace) -> bool:
        """True when this workspace should drive the graph-first path.

        Gated exactly like the search runner resolution (``runner.get_search_runner``):
        the feature must be on (``scg.enabled``) AND at least one of the
        workspace's sources must be mapped in the SCG store — otherwise the
        graph routes nothing and the run is better served by the default
        grounding path. A graph-less install (no SCG engine) reports not-eligible
        and falls back, never crashes.
        """
        if not ScgConfig.enabled():
            return False
        return self._has_mapped_source(workspace)

    def build_responder(
        self,
        workspace: Workspace,
        *,
        runtime: Any,
        schema: dict[str, object],
        tools: list[str] | None,
        source_platform: str | None,
        project: str | None = None,
    ) -> StructuredResponder:
        """Compose the graph-first :class:`StructuredResponder` for *workspace*.

        The run grant resolves from the workspace's #75 virtual MCP config
        (attached server names) first, falling back to the workspace's raw
        ``sources`` — identical to ``SearchRun.start``. The caller-supplied
        ``tools`` (if any) intersect the binding's allowed tools so a caller can
        still narrow, never widen, the grant.
        """
        grant_sources = (
            WorkspaceMcpConfig.attached_server_names(self.store, workspace.id)
            or list(workspace.sources)
        )
        connector_grant = SourceCatalog.tools_for(grant_sources, project)
        binding = WorkspaceGraphBinding.for_workspace(workspace, connector_grant)

        allowed = binding.allowed_tools()
        if tools:
            # A caller narrows (never widens) the grant: intersect, preserving
            # the binding's order so the traversal verbs survive if requested.
            narrow = set(tools)
            allowed = [t for t in allowed if t in narrow]

        return StructuredResponder(
            runtime=runtime,
            schema=schema,
            workspace=workspace.id,
            allowed_tools=allowed,
            source_platform=source_platform,
            capabilities=binding.capabilities,
            context_events=binding.context_events,
            extra_instructions=load_playbook(_STRUCTURED_PLAYBOOK),
            scope_factory=binding.scope,
        )

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _has_mapped_source(workspace: Workspace) -> bool:
        """True iff the SCG store holds at least one of the workspace's sources.

        Import-guarded so a graph-less install degrades to ``False`` (fall back
        to default grounding) rather than raising.
        """
        try:
            from mewbo_graph.scg.store import get_scg_store
        except ImportError:
            return False
        try:
            store = get_scg_store()
            mapped = {s.source_id for s in store.list_sources()}
        except Exception as exc:  # noqa: BLE001 — a store hiccup is not eligible
            logging.debug("scg mapped-source probe failed: {}", exc)
            return False
        return any(src in mapped for src in workspace.sources)


__all__ = ["GraphStructuredRunner"]
