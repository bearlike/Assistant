/**
 * GraphIndex — a trivial Map-based adjacency index over a {@link KnowledgeGraph}.
 *
 * Panels look up a node by id, its incoming/outgoing edges, and a folder/file's
 * direct children. Three Maps built in one constructor pass cover all of it, so
 * panels never re-scan the O(E) edge list. No graph library — a couple of Maps
 * is the right tool.
 */

import type {
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "../api/types";

/** A graph node, as the panels read it (aliased to the wire node). */
export type InspectorNode = KnowledgeGraphNode;

/** One end of an edge as seen from a focal node. */
export interface AdjacentEdge {
  edge: KnowledgeGraphEdge;
  /** The node at the OTHER end of the edge (resolved, may be undefined if dangling). */
  other: InspectorNode | undefined;
  /** Id of the other end (always present even when the node is missing). */
  otherId: string;
}

/** A node's incoming + outgoing edges, resolved against the index. */
export interface Adjacency {
  incoming: AdjacentEdge[];
  outgoing: AdjacentEdge[];
}

const EMPTY_ADJACENCY: Adjacency = { incoming: [], outgoing: [] };

export class GraphIndex {
  // ── State (the three Maps) ───────────────────────────────────────────
  private readonly byId = new Map<string, InspectorNode>();
  /** parentId → direct child nodes (folder/file containment tree). */
  private readonly childrenByParent = new Map<string, InspectorNode[]>();
  /** node id → its incoming/outgoing edges. */
  private readonly adjacency = new Map<string, Adjacency>();

  // ── Construction (single O(N+E) pass) ───────────────────────────────
  constructor(graph: KnowledgeGraph) {
    for (const n of graph.nodes) {
      this.byId.set(n.data.id, n);

      // `parentId` is `string | null` (null = visible root) — truthiness
      // excludes both null and undefined.
      const parentId = n.data.parentId;
      if (parentId) {
        const list = this.childrenByParent.get(parentId);
        if (list) list.push(n);
        else this.childrenByParent.set(parentId, [n]);
      }
    }

    for (const edge of graph.edges) {
      const { source, target } = edge.data;
      this.edgesFor(source).outgoing.push({
        edge,
        otherId: target,
        other: this.byId.get(target),
      });
      this.edgesFor(target).incoming.push({
        edge,
        otherId: source,
        other: this.byId.get(source),
      });
    }
  }

  // ── Lookups ──────────────────────────────────────────────────────────

  /** The node with this id, or undefined. O(1). */
  node(id: string): InspectorNode | undefined {
    return this.byId.get(id);
  }

  /** Incoming + outgoing edges for a node id. O(1) — built in the constructor. */
  edgesOf(id: string): Adjacency {
    return this.adjacency.get(id) ?? EMPTY_ADJACENCY;
  }

  /** Direct children of a folder/file node (by `parentId`). O(1). */
  children(parentId: string): InspectorNode[] {
    return this.childrenByParent.get(parentId) ?? [];
  }

  // ── Private ──────────────────────────────────────────────────────────

  /** Get-or-create the (mutable) adjacency record for an id. */
  private edgesFor(id: string): Adjacency {
    let adj = this.adjacency.get(id);
    if (!adj) {
      adj = { incoming: [], outgoing: [] };
      this.adjacency.set(id, adj);
    }
    return adj;
  }
}
