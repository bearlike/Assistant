/**
 * Workspace SCG graph viewer (#79) — the search-side capability graph, rendered
 * inside a shadcn ``Dialog`` so it overlays the search surface from a workspace
 * card / results rail entry point.
 *
 * It REUSES the shared 3D ``Graph3DView`` engine wholesale (the SAME WebGL
 * galaxy the wiki Knowledge Graph renders) by injecting the search-domain
 * ``SCG_GRAPH_THEME`` and a node inspector — no per-domain renderer, no
 * Cytoscape. This component owns only the React lifecycle, the SCG node
 * inspector (capability schema / recipe / anchored memory notes), and the
 * unmapped-ghost "map this source" hint. The SCG graph has no folder hierarchy,
 * so it flows through the engine's collapse model as a pure pass-through.
 */
import { useMemo, type ReactNode } from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { cn } from "@/lib/utils";
import {
  Graph3DView,
  type Graph3DInspectorCtx,
  type Graph3DPick,
  type Graph3DStats,
} from "../../wiki/Graph3DView";
import { GraphIndex, type AdjacentEdge } from "../../wiki/inspector/GraphIndex";
import type { KnowledgeGraph } from "../../wiki/api/types";
import { useWorkspaceGraph } from "../../../hooks/useAgenticSearch";
import type { Workspace } from "../../../types/agenticSearch";
import { SCG_GRAPH_THEME, SCG_KIND_DOT, SCG_KIND_LABEL } from "./scgGraphConfig";
import type {
  ScgEdgeKind,
  ScgGraphLayer,
  ScgGraphNode,
  ScgNodeKind,
  WorkspaceGraph,
} from "./types";

interface WorkspaceGraphDialogProps {
  open: boolean;
  onClose: () => void;
  workspace: Workspace;
  /** Open the Sources flow to map an unmapped source (the map action lives
   *  there — we link, never rebuild it). */
  onMapSource?: () => void;
}

/** One edge as the inspector lists it (kind + the other endpoint). */
interface EdgeInfo {
  kind: ScgEdgeKind;
  otherId: string;
  otherLabel: string;
}

interface SelectedScgNode {
  id: string;
  label: string;
  kind: ScgNodeKind;
  layer: ScgGraphLayer;
  sourceId?: string;
  doc?: string;
  snippet?: string;
  labels?: string[];
  unmapped?: boolean;
  degree: number;
  inEdges: EdgeInfo[];
  outEdges: EdgeInfo[];
}

export function WorkspaceGraphDialog({
  open,
  onClose,
  workspace,
  onMapSource,
}: WorkspaceGraphDialogProps) {
  return (
    <Dialog open={open} onOpenChange={(o) => !o && onClose()}>
      <DialogContent className="max-w-[min(96vw,1200px)] w-[96vw] h-[88vh] p-0 gap-0 flex flex-col overflow-hidden">
        <DialogHeader className="px-4 py-3 border-b border-[hsl(var(--border))] flex-row items-center gap-2 space-y-0">
          <DialogTitle className="text-sm font-medium truncate">
            {workspace.name} — capability graph
          </DialogTitle>
          <DialogDescription className="sr-only">
            The Source Capability Graph for this workspace: capability, type, and
            field nodes from its mapped sources, the connector memory layer, and
            unmapped sources shown as ghost nodes.
          </DialogDescription>
        </DialogHeader>
        {open && <GraphBody workspace={workspace} onMapSource={onMapSource} />}
      </DialogContent>
    </Dialog>
  );
}

/** Split out so the 3D engine only mounts while the dialog is open. */
function GraphBody({
  workspace,
  onMapSource,
}: {
  workspace: Workspace;
  onMapSource?: () => void;
}) {
  const query = useWorkspaceGraph(workspace.id);
  const graph = query.data ?? null;

  const stats = useMemo<Graph3DStats | null>(
    () =>
      graph
        ? {
            nodeCount: graph.stats.totalNodes,
            edgeCount: graph.stats.totalEdges,
            kinds: graph.stats.kinds,
          }
        : null,
    [graph],
  );

  // One adjacency index per graph (the query cache hands back a stable ref).
  // The cast is the structural-compat seam — GraphIndex reads only id/endpoints.
  const index = useMemo(
    () => (graph ? new GraphIndex(graph as unknown as KnowledgeGraph) : null),
    [graph],
  );

  const allUnmapped =
    !!graph && graph.nodes.length > 0 && graph.nodes.every((n) => n.data.unmapped);

  const banner =
    allUnmapped ? (
      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))]/95 px-3 py-2 text-center text-[11px] text-[hsl(var(--muted-foreground))] shadow-[var(--elev-1)] [text-wrap:balance]">
        None of this workspace's sources are mapped yet — map a source to build
        its capability subgraph.
        {onMapSource && (
          <button
            type="button"
            onClick={onMapSource}
            className="ml-1 text-[hsl(var(--primary))] hover:underline"
          >
            Open Sources
          </button>
        )}
      </div>
    ) : undefined;

  // SCG inspects nodes only (no edge panel). A node pick is resolved to its
  // schema/recipe/memory detail + 1-hop edges via the shared GraphIndex.
  const renderInspector = (pick: Graph3DPick, ctx: Graph3DInspectorCtx): ReactNode => {
    if (!pick.node || !index) return null;
    const d = pick.node as unknown as ScgGraphNode["data"];
    const adj = index.edgesOf(pick.node.id);
    const toInfo = (a: AdjacentEdge): EdgeInfo => ({
      kind: a.edge.data.kind as unknown as ScgEdgeKind,
      otherId: a.otherId,
      otherLabel: a.other?.data.label ?? a.otherId,
    });
    const selected: SelectedScgNode = {
      id: d.id,
      label: d.label,
      kind: d.kind,
      layer: d.layer,
      sourceId: d.sourceId,
      doc: d.doc,
      snippet: d.snippet,
      labels: d.labels,
      unmapped: d.unmapped,
      degree: adj.incoming.length + adj.outgoing.length,
      inEdges: adj.incoming.map(toInfo),
      outEdges: adj.outgoing.map(toInfo),
    };
    return <NodeInspector node={selected} onClose={ctx.onClose} onMapSource={onMapSource} />;
  };

  return (
    <Graph3DView
      graph={graph}
      stats={stats}
      isPending={query.isPending}
      isError={query.isError}
      theme={SCG_GRAPH_THEME}
      renderInspector={renderInspector}
      banner={banner}
      hint="Drag to orbit · scroll to zoom"
      labels={{
        loading: "Loading capability graph…",
        error: "Couldn't load the workspace graph.",
        empty: "This workspace has no sources to map yet.",
      }}
    />
  );
}

/** Right-rail inspector: capability schema / recipe detail + anchored notes. */
function NodeInspector({
  node,
  onClose,
  onMapSource,
}: {
  node: SelectedScgNode;
  onClose: () => void;
  onMapSource?: () => void;
}) {
  return (
    <aside className="w-[320px] shrink-0 border-l border-[hsl(var(--border))] bg-[hsl(var(--card))] flex flex-col">
      <header className="px-3 py-2 border-b border-[hsl(var(--border))] flex items-center gap-2">
        <span className={cn("w-2.5 h-2.5 rounded-full", SCG_KIND_DOT[node.kind])} />
        <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          {SCG_KIND_LABEL[node.kind]}
        </span>
        <span className="text-xs font-mono truncate flex-1">{node.label}</span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </header>
      <div className="px-3 py-3 space-y-3 overflow-y-auto text-xs">
        {node.sourceId && (
          <Field label="Source">
            <code className="font-mono break-all">{node.sourceId}</code>
          </Field>
        )}
        {node.unmapped ? (
          <div className="space-y-2">
            <p className="text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">
              This source hasn't been mapped into the Source Capability Graph yet,
              so its capabilities aren't searchable.
            </p>
            {onMapSource && (
              <Button variant="primary" size="sm" onClick={onMapSource} className="w-full">
                Map this source
              </Button>
            )}
          </div>
        ) : (
          <>
            {node.doc && (
              <Field label="Description">
                <p className="whitespace-pre-wrap text-[hsl(var(--muted-foreground))]">
                  {node.doc}
                </p>
              </Field>
            )}
            {node.snippet && (
              <Field label="Reachability note">
                <p className="whitespace-pre-wrap text-[hsl(var(--muted-foreground))]">
                  {node.snippet}
                </p>
              </Field>
            )}
            {node.labels && node.labels.length > 0 && (
              <Field label="Labels">
                <div className="flex flex-wrap gap-1">
                  {node.labels.map((l) => (
                    <span
                      key={l}
                      className="px-1.5 py-px rounded-full text-[10px] bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))] font-mono"
                    >
                      {l}
                    </span>
                  ))}
                </div>
              </Field>
            )}
            <Field label="Connections">
              <span className="font-mono">{node.degree}</span>
            </Field>
            {node.outEdges.length > 0 && (
              <EdgeSection title={`Outgoing (${node.outEdges.length})`} edges={node.outEdges} />
            )}
            {node.inEdges.length > 0 && (
              <EdgeSection title={`Incoming (${node.inEdges.length})`} edges={node.inEdges} />
            )}
          </>
        )}
      </div>
    </aside>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
        {label}
      </div>
      {children}
    </div>
  );
}

function EdgeSection({ title, edges }: { title: string; edges: EdgeInfo[] }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
        {title}
      </div>
      <ul className="space-y-0.5">
        {edges.slice(0, 14).map((e, i) => (
          <li key={`${e.otherId}-${i}`} className="flex items-center gap-1.5 truncate">
            <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] shrink-0">
              {e.kind}
            </span>
            <span className="font-mono truncate text-[11px]">{e.otherLabel || e.otherId}</span>
          </li>
        ))}
        {edges.length > 14 && (
          <li className="text-[10px] text-[hsl(var(--muted-foreground))]">
            +{edges.length - 14} more
          </li>
        )}
      </ul>
    </div>
  );
}

export type { WorkspaceGraph };
