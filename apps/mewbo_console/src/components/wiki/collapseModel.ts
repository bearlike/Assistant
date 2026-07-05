/**
 * CollapseModel — the pure level-of-detail brain behind the 3D galaxy (the
 * ``?hierarchy=1`` payload). This is the ONE piece of genuine app logic worth
 * isolating + unit-testing; everything else is off-the-shelf
 * ``react-force-graph-3d``.
 *
 * The folder tree IS the spatial scaffold AND the rendering budget: folders
 * are collapsible supernodes, collapsed by default except the top level, so a
 * 17K-node repo never renders 17K spheres at once. The screen holds the
 * ``expanded`` set as React state and asks this class to derive the visible
 * graph from it: ``visibleGraph(expanded)`` is a PURE function of the indices
 * built once in the constructor + the passed set (no internal mutable
 * expansion state), so the screen can
 * ``useMemo(() => model.visibleGraph(expanded), [graph, expanded])`` and feed
 * the result straight to ``<ForceGraph3D graphData=…>``.
 *
 * Visibility rule: a node is visible iff every ancestor folder up to a visible
 * root is expanded. A collapsed folder is itself visible (a single supernode)
 * but its descendants are not. Any relationship edge whose endpoint is hidden
 * inside a collapsed folder is re-pointed onto that collapsed supernode (its
 * nearest visible ancestor); duplicate supernode↔supernode ties collapse into
 * ONE weighted edge that carries its constituents so the inspector can expand
 * the aggregate.
 *
 * The ``CONTAINS`` tree is kept too — re-pointed the SAME way into faint, thin
 * containment ties (``data.containment``) so the hierarchy backbone stays
 * visible and nothing floats — but deduped and NEVER aggregated, so the
 * relationship edges remain the visual focus.
 */
import type {
  GraphEdgeKind,
  KnowledgeGraph,
  KnowledgeGraphEdge,
  KnowledgeGraphNode,
} from "./api/types";

/** An edge in the derived visible graph. Re-pointed/aggregated ties carry
 *  extra fields the inspector reads; plain ties pass through unchanged. */
export interface CollapsedEdge extends KnowledgeGraphEdge {
  data: KnowledgeGraphEdge["data"] & {
    /** How many original edges this tie represents (1 = passthrough). */
    weight?: number;
    /** The constituent original edges folded into an aggregated tie. */
    aggregated?: KnowledgeGraphEdge[];
    /** Set on the faint hierarchy-backbone ties derived from ``CONTAINS``
     *  (folder→file→symbol, folder→subfolder), re-pointed onto visible
     *  representatives but NEVER aggregated into weighted ties. The screen
     *  styles these subordinate to real relationship edges. */
    containment?: boolean;
  };
}

export interface VisibleGraph {
  nodes: KnowledgeGraphNode[];
  edges: CollapsedEdge[];
}

export class CollapseModel {
  // ── State (immutable indices built once) ─────────────────────────
  private readonly nodes: Map<string, KnowledgeGraphNode>;
  /** Non-CONTAINS edges (real relationships) — the visual focus. */
  private readonly relEdges: KnowledgeGraphEdge[];
  /** CONTAINS edges (the directory/symbol tree). Rendered as faint, thin
   *  containment ties so the hierarchy backbone is visible and nothing floats —
   *  re-pointed onto visible representatives, never aggregated. */
  private readonly containEdges: KnowledgeGraphEdge[];
  /** parentId per node id (the folder it lives under, or null = root). */
  private readonly parent: Map<string, string | null>;
  /** Folder ids only. */
  private readonly folders: Set<string>;

  // ── Construction ─────────────────────────────────────────────────

  constructor(graph: KnowledgeGraph) {
    this.nodes = new Map(graph.nodes.map((n) => [n.data.id, n]));
    this.parent = new Map();
    this.folders = new Set();
    for (const n of graph.nodes) {
      this.parent.set(n.data.id, n.data.parentId ?? null);
      if (n.data.kind === "Folder") this.folders.add(n.data.id);
    }
    this.relEdges = graph.edges.filter((e) => e.data.kind !== "CONTAINS");
    this.containEdges = graph.edges.filter((e) => e.data.kind === "CONTAINS");
  }

  /** The default expansion: top-level (depth 1) folders only — a folder whose
   *  parent is null. Everything deeper starts collapsed so the first frame is
   *  bounded. The screen seeds its ``expanded`` React state with this. */
  static initialExpanded(graph: KnowledgeGraph): Set<string> {
    const out = new Set<string>();
    for (const n of graph.nodes) {
      if (n.data.kind === "Folder" && (n.data.parentId ?? null) === null) {
        out.add(n.data.id);
      }
    }
    return out;
  }

  // ── Queries ──────────────────────────────────────────────────────

  isFolder(id: string): boolean {
    return this.folders.has(id);
  }

  /** Recursive descendant count per folder id — drives supernode sizing + the
   *  hover caption. Computed once; safe to memoise at the call site. */
  foldedCounts(): Map<string, number> {
    const childrenOf = new Map<string, string[]>();
    for (const [id, p] of this.parent) {
      if (p == null) continue;
      const list = childrenOf.get(p);
      if (list) list.push(id);
      else childrenOf.set(p, [id]);
    }
    const memo = new Map<string, number>();
    const count = (id: string): number => {
      const cached = memo.get(id);
      if (cached != null) return cached;
      let total = 0;
      for (const child of childrenOf.get(id) ?? []) total += 1 + count(child);
      memo.set(id, total);
      return total;
    };
    const out = new Map<string, number>();
    for (const id of this.folders) out.set(id, count(id));
    return out;
  }

  // ── Derivation (PURE over the passed ``expanded`` set) ────────────

  /**
   * Derive the renderable graph for *expanded*: the set of visible nodes plus
   * edges re-pointed onto collapsed supernodes and de-duplicated into weighted
   * ties. Pure — same ``(graph, expanded)`` always yields the same result.
   */
  visibleGraph(expanded: Set<string>): VisibleGraph {
    const visible = new Set<string>();
    for (const id of this.nodes.keys()) {
      if (this.isVisible(id, expanded)) visible.add(id);
    }
    const nodes: KnowledgeGraphNode[] = [];
    for (const id of visible) {
      const n = this.nodes.get(id);
      if (n) nodes.push(n);
    }
    return {
      nodes,
      edges: [
        ...this.collapseEdges(visible, expanded),
        ...this.collapseContainment(visible, expanded),
      ],
    };
  }

  /** A node is visible iff every ancestor folder is expanded. */
  private isVisible(id: string, expanded: Set<string>): boolean {
    let p = this.parent.get(id) ?? null;
    while (p != null) {
      if (this.folders.has(p) && !expanded.has(p)) return false;
      p = this.parent.get(p) ?? null;
    }
    return true;
  }

  /**
   * Walk up from *id* to its nearest visible representative: a node inside a
   * collapsed folder is shown as the deepest collapsed ancestor folder, so
   * edges to *id* must re-point there. A visible node resolves to itself.
   */
  resolveVisible(id: string, expanded: Set<string>): string {
    let resolved = id;
    let p = this.parent.get(id) ?? null;
    while (p != null) {
      if (this.folders.has(p) && !expanded.has(p)) resolved = p;
      p = this.parent.get(p) ?? null;
    }
    return resolved;
  }

  /**
   * Re-point every relationship edge onto its endpoints' visible
   * representatives, drop self-loops created by the fold, then aggregate
   * duplicate (source,target,kind) ties into ONE weighted edge carrying its
   * constituent originals.
   */
  private collapseEdges(
    visible: Set<string>,
    expanded: Set<string>,
  ): CollapsedEdge[] {
    const buckets = new Map<
      string,
      { source: string; target: string; kind: GraphEdgeKind; members: KnowledgeGraphEdge[] }
    >();

    for (const e of this.relEdges) {
      const s = this.resolveVisible(e.data.source, expanded);
      const t = this.resolveVisible(e.data.target, expanded);
      if (s === t || !visible.has(s) || !visible.has(t)) continue;
      const key = `${s} ${t} ${e.data.kind}`;
      const bucket = buckets.get(key);
      if (bucket) bucket.members.push(e);
      else buckets.set(key, { source: s, target: t, kind: e.data.kind, members: [e] });
    }

    const out: CollapsedEdge[] = [];
    for (const [key, b] of buckets) {
      if (b.members.length === 1) {
        const orig = b.members[0];
        // A singleton whose endpoints weren't re-pointed passes through
        // verbatim; one re-pointed onto supernodes keeps the original id/label
        // but carries the supernode endpoints.
        if (orig.data.source === b.source && orig.data.target === b.target) {
          out.push(orig as CollapsedEdge);
        } else {
          out.push({ data: { ...orig.data, source: b.source, target: b.target } });
        }
        continue;
      }
      out.push({
        data: {
          id: `agg:${key}`,
          source: b.source,
          target: b.target,
          kind: b.kind,
          weight: b.members.length,
          aggregated: b.members,
        },
      });
    }
    return out;
  }

  /**
   * Derive the faint hierarchy-backbone ties from ``CONTAINS`` edges. Each
   * endpoint is re-pointed onto its visible representative via the SAME collapse
   * logic relationship edges use, so a tie appears only when the parent is
   * expanded and the child is visible (a collapsed folder absorbs its whole
   * subtree → those internal ties fold into a self-loop and are dropped).
   * Duplicates are deduped; unlike relationships these are NEVER aggregated into
   * weighted ties — the backbone stays subordinate, one thin line per pair.
   */
  private collapseContainment(
    visible: Set<string>,
    expanded: Set<string>,
  ): CollapsedEdge[] {
    const seen = new Set<string>();
    const out: CollapsedEdge[] = [];
    for (const e of this.containEdges) {
      const s = this.resolveVisible(e.data.source, expanded);
      const t = this.resolveVisible(e.data.target, expanded);
      if (s === t || !visible.has(s) || !visible.has(t)) continue;
      const key = `${s} ${t}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        data: {
          id: `contain:${key}`,
          source: s,
          target: t,
          kind: "CONTAINS",
          layer: e.data.layer,
          containment: true,
        },
      });
    }
    return out;
  }
}
