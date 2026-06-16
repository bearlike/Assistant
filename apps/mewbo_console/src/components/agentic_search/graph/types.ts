// Wire types for the workspace SCG multiplex graph (#79).
//
// Mirrors the API ``GET /api/agentic_search/workspaces/<id>/graph`` payload,
// which wraps ``mewbo_graph.scg.graph_view.ScgGraphView.to_wire()`` (#76) and
// normalizes schema-edge endpoints to node ids + appends unmapped ghost nodes.
// The shape parallels the wiki ``KnowledgeGraph`` so it feeds the SAME
// ``KnowledgeGraphRenderer`` engine — only the kind/edge/layer vocabulary
// differs (the search SCG vs the wiki code graph).
//
// All unions are CLOSED so every ``Record<Kind, …>`` map in the config + screen
// is exhaustive and ``tsc`` flags a missing arm (the console convention).

/** SCG schema-node kinds + the multiplex memory kind + the FE-only ghost. */
export type ScgNodeKind =
  | "capability" // an executable op (MCP tool / OpenAPI endpoint / procedure)
  | "entity_type" // a schema type a source exposes
  | "field" // a property on an entity type
  | "route_recipe" // a precomputed pathway through capabilities
  | "Memory" // a connector reachability note (memory layer)
  | "unmapped"; // FE-only: a workspace source with no SCG graph yet (ghost)

/** SCG schema-edge kinds + the multiplex memory/cross kinds. */
export type ScgEdgeKind =
  | "HAS_ENTITY"
  | "HAS_FIELD"
  | "SUPPORTS_QUERY"
  | "PRODUCES"
  | "CONSUMES"
  | "RESOLVES_TO"
  | "ANCHORS" // cross-layer: memory note → schema node
  | "RELATES"; // note ↔ note

/** Multiplex layer a node belongs to (schema | memory | entity). */
export type ScgGraphLayer = "schema" | "memory" | "entity";

/** Layer an edge belongs to — ``cross`` is the inter-layer ANCHORS tie. */
export type ScgGraphEdgeLayer = ScgGraphLayer | "cross";

export interface ScgGraphNode {
  data: {
    id: string;
    label: string;
    kind: ScgNodeKind;
    layer: ScgGraphLayer;
    /** Schema nodes — the source this capability/type/field belongs to. */
    sourceId?: string;
    /** Schema nodes — the SCG source_key (addressing key; not rendered). */
    sourceKey?: string;
    /** Schema nodes — the capability/type/field doc string. */
    doc?: string;
    /** Memory nodes — the stored reachability-fact snippet. */
    snippet?: string;
    /** Memory nodes — free-form classifier labels. */
    labels?: string[];
    /** ``true`` on a ghost node for an unmapped workspace source. */
    unmapped?: boolean;
  };
}

export interface ScgGraphEdge {
  data: {
    id: string;
    source: string;
    target: string;
    kind: ScgEdgeKind;
    layer: ScgGraphEdgeLayer;
    /** Schema edges — the parser-asserted weight. */
    weight?: number;
  };
}

export interface WorkspaceGraphStats {
  totalNodes: number;
  totalEdges: number;
  kinds: Partial<Record<ScgNodeKind, number>>;
  perLayer: Record<ScgGraphLayer, number>;
  /** Workspace sources with no SCG graph yet (rendered as ghost nodes). */
  unmapped: string[];
}

export interface WorkspaceGraph {
  /** The resolved source-id scope this view was assembled for. */
  scope: string[];
  nodes: ScgGraphNode[];
  edges: ScgGraphEdge[];
  stats: WorkspaceGraphStats;
}

/**
 * `GET /workspaces/<id>/graph/summary` — the graph's `scope` + `stats` only
 * (no node/edge arrays). The landing health band reads four numbers off
 * `stats`, so it fetches this instead of the full graph (#139).
 */
export type WorkspaceGraphSummary = Pick<WorkspaceGraph, "scope" | "stats">;
