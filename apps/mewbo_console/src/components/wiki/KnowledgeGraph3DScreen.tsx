/**
 * KnowledgeGraph3DScreen — the "Code Galaxy" wiki graph view
 * (``/wiki/graph?slug=…``). A THIN adapter over the shared {@link Graph3DView}:
 * it fetches the ``?hierarchy=1`` knowledge graph, assembles the wiki
 * {@link Graph3DTheme} from the shared ``graphTheme`` maps, and wires clicks to
 * the typed {@link GraphInspector} registry. All the rendering (WebGL galaxy,
 * folder collapse, toolbar, camera) lives in ``Graph3DView`` — the SAME engine
 * the Agentic-Search SCG graph uses, so there is one implementation, not two.
 */
import { useMemo, type ReactNode } from "react";

import { WikiTopBar } from "./WikiTopBar";
import { useKnowledgeGraph } from "./api/hooks";
import type {
  GraphNodeKind,
  GraphSelection,
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "./api/types";
import {
  ALL_NODE_KINDS,
  EDGE_CONTAIN_VAR,
  EDGE_VAR,
  FOLDER_VAR,
  KIND_DOT,
  KIND_LAYER,
  KIND_VAR,
  LAYER_DOT,
  LAYER_LABEL,
  LAYER_ORDER,
} from "./graphTheme";
import {
  Graph3DView,
  type Graph3DInspectorCtx,
  type Graph3DNode,
  type Graph3DPick,
  type Graph3DStats,
  type Graph3DTheme,
} from "./Graph3DView";
import { GraphInspector } from "./inspector/GraphInspector";
import type { PlatformId } from "./router";

interface KnowledgeGraph3DScreenProps {
  slug?: string;
  platform?: PlatformId;
}

/** The wiki visual vocabulary, assembled once from the shared theme maps. */
const WIKI_GRAPH_THEME: Graph3DTheme = {
  allKinds: ALL_NODE_KINDS,
  kindVar: KIND_VAR,
  kindDot: KIND_DOT,
  // Wiki kind chips read the kind name verbatim (no friendlier label).
  kindLabel: Object.fromEntries(ALL_NODE_KINDS.map((k) => [k, k])),
  edgeVar: EDGE_VAR,
  kindLayer: KIND_LAYER,
  layerOrder: LAYER_ORDER,
  layerLabel: LAYER_LABEL,
  layerDot: LAYER_DOT,
  folderVar: FOLDER_VAR,
  edgeContainVar: EDGE_CONTAIN_VAR,
  nodeLabel: (n, folded) =>
    n.kind === "Folder"
      ? `${n.folderPath ?? n.label} · ${folded} items`
      : n.file
        ? `${n.label} — ${n.file}`
        : n.label,
};

/** A force-graph link endpoint is an id before linking, the resolved node
 *  after — the edge selection needs the id form. */
function endId(end: string | Graph3DNode): string {
  return typeof end === "string" ? end : end.id;
}

/** Map a node kind to the inspector's coarse selection bucket. */
function selectionKind(kind: GraphNodeKind): GraphSelection["kind"] {
  switch (kind) {
    case "Folder":
      return "folder";
    case "File":
      return "file";
    case "Module":
      return "module";
    case "External":
      return "external";
    case "Entity":
      return "entity";
    case "Memory":
      return "memory";
    default:
      return "symbol"; // Class / Function / Method / Interface
  }
}

export function KnowledgeGraph3DScreen({ slug, platform }: KnowledgeGraph3DScreenProps) {
  const query = useKnowledgeGraph(slug ?? null, { hierarchy: true });
  const graph = query.data ?? null;

  const stats = useMemo<Graph3DStats | null>(
    () =>
      graph
        ? {
            nodeCount: graph.stats.nodeCount,
            edgeCount: graph.stats.edgeCount,
            folderCount: graph.stats.folderCount,
            kinds: graph.stats.kinds,
          }
        : null,
    [graph],
  );

  // Click → the typed GraphInspector registry. A node maps to its coarse
  // selection bucket; a link maps to an ``edge`` selection carrying the
  // (possibly aggregated) constituents.
  const renderInspector = (pick: Graph3DPick, ctx: Graph3DInspectorCtx): ReactNode => {
    let selection: GraphSelection | null = null;
    if (pick.node) {
      selection = {
        kind: selectionKind(pick.node.kind as GraphNodeKind),
        node: { data: pick.node as unknown as KnowledgeGraphNode["data"] },
      };
    } else if (pick.link) {
      const l = pick.link;
      selection = {
        kind: "edge",
        edge: {
          data: {
            ...l,
            source: endId(l.source),
            target: endId(l.target),
          } as unknown as KnowledgeGraphEdge["data"],
        },
        aggregated: l.aggregated && l.aggregated.length > 0 ? l.aggregated : undefined,
      };
    }
    if (!selection) return null;
    return (
      <GraphInspector
        selection={selection}
        graph={ctx.graph as unknown as KnowledgeGraph}
        onNavigate={ctx.onNavigate}
        onClose={ctx.onClose}
        slug={slug}
        platform={platform}
      />
    );
  };

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <WikiTopBar repo={slug} showBackToAll showSettings />
      <Graph3DView
        graph={graph}
        stats={stats}
        isPending={query.isPending}
        isError={query.isError}
        theme={WIKI_GRAPH_THEME}
        renderInspector={renderInspector}
        hint="Click a folder to expand · drag to orbit"
        labels={{
          loading: "Loading code galaxy…",
          error: "Graph unavailable. Re-index this project and try again.",
          empty: "No graph data persisted for this project yet.",
        }}
      />
    </div>
  );
}
