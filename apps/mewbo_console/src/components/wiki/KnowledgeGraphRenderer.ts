/**
 * KnowledgeGraphRenderer — atomic class around a Cytoscape instance.
 *
 * State (cy handle, container, filter sets, focus id, raw graph) lives on
 * the instance; behaviour over that state is exposed as instance methods;
 * pure helpers (icon URIs, kind palette, layout config, stylesheet) are
 * static. Mounting Cytoscape directly avoids the ``react-cytoscapejs``
 * canvas-rebuild trap.
 *
 * Visual model: Neo4j-Bloom-lite. Each node is a coloured disc carrying a
 * white Lucide glyph for its kind; size is degree-weighted. Click a node →
 * focus mode (1-hop highlighted, rest dimmed, viewport zoomed to fit the
 * neighbourhood). Tap empty canvas to release.
 */
import cytoscape from "cytoscape";
import type { Core, ElementDefinition, EventObject } from "cytoscape";
import fcose from "cytoscape-fcose";

import type {
  GraphEdgeKind,
  GraphNodeKind,
  KnowledgeGraph,
} from "./api/types";

// Register fcose once per module load (idempotent in modern Cytoscape).
let _fcoseRegistered = false;
function ensureFcose(): void {
  if (_fcoseRegistered) return;
  cytoscape.use(fcose);
  _fcoseRegistered = true;
}

// ── Lucide icon paths (ISC, copied from lucide-react@0.522 internals) ──
// Inlined to avoid a fragile reach into ``lucide-react/dist/esm/icons/*.js``
// internals or pulling react-dom/server. Six paths × ~4 elements each.
type IconElement = ["path" | "rect" | "polyline", Record<string, string>];

const ICON_PATHS: Record<GraphNodeKind, IconElement[]> = {
  File: [
    ["path", { d: "M10 12.5 8 15l2 2.5" }],
    ["path", { d: "m14 12.5 2 2.5-2 2.5" }],
    ["path", { d: "M14 2v4a2 2 0 0 0 2 2h4" }],
    ["path", { d: "M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7z" }],
  ],
  Class: [
    ["path", { d: "M21 8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z" }],
    ["path", { d: "m3.3 7 8.7 5 8.7-5" }],
    ["path", { d: "M12 22V12" }],
  ],
  Function: [
    ["rect", { width: "18", height: "18", x: "3", y: "3", rx: "2", ry: "2" }],
    ["path", { d: "M9 17c2 0 2.8-1 2.8-2.8V10c0-2 1-3.3 3.2-3" }],
    ["path", { d: "M9 11.2h5.7" }],
  ],
  Method: [
    ["path", { d: "M8 3H7a2 2 0 0 0-2 2v5a2 2 0 0 1-2 2 2 2 0 0 1 2 2v5c0 1.1.9 2 2 2h1" }],
    ["path", { d: "M16 21h1a2 2 0 0 0 2-2v-5c0-1.1.9-2 2-2a2 2 0 0 1-2-2V5a2 2 0 0 0-2-2h-1" }],
  ],
  Interface: [
    ["rect", { width: "8", height: "8", x: "3", y: "3", rx: "2" }],
    ["path", { d: "M7 11v4a2 2 0 0 0 2 2h4" }],
    ["rect", { width: "8", height: "8", x: "13", y: "13", rx: "2" }],
  ],
  Module: [
    ["path", { d: "M11 21.73a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73z" }],
    ["path", { d: "M12 22V12" }],
    ["polyline", { points: "3.29 7 12 12 20.71 7" }],
    ["path", { d: "m7.5 4.27 9 5.15" }],
  ],
};

function makeIconUri(paths: IconElement[]): string {
  const inner = paths
    .map(([tag, attrs]) => {
      const a = Object.entries(attrs)
        .map(([k, v]) => `${k}="${v}"`)
        .join(" ");
      return `<${tag} ${a} />`;
    })
    .join("");
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" ` +
    `viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" ` +
    `stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
  return `data:image/svg+xml;utf8,${encodeURIComponent(svg)}`;
}

const ICON_URI: Record<GraphNodeKind, string> = {
  File: makeIconUri(ICON_PATHS.File),
  Class: makeIconUri(ICON_PATHS.Class),
  Function: makeIconUri(ICON_PATHS.Function),
  Method: makeIconUri(ICON_PATHS.Method),
  Interface: makeIconUri(ICON_PATHS.Interface),
  Module: makeIconUri(ICON_PATHS.Module),
};

// Theme-aware kind palette — both blocks live in src/index.css.
const KIND_VAR: Record<GraphNodeKind, string> = {
  File: "--graph-file",
  Module: "--graph-module",
  Class: "--graph-class",
  Function: "--graph-function",
  Method: "--graph-method",
  Interface: "--graph-interface",
};

const EDGE_VAR: Record<GraphEdgeKind, string> = {
  CONTAINS: "--graph-edge-soft",
  IMPORTS: "--graph-file",
  CALLS: "--graph-function",
  EXTENDS: "--graph-class",
  REFERENCES: "--graph-edge-soft",
};

function cssVar(name: string): string {
  if (typeof window === "undefined") return "#999";
  const raw = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  if (!raw) return "#999";
  // Cytoscape's <canvas>-side colour parser accepts CSS Level 3 comma
  // syntax (`hsl(H, S%, L%)`) but silently drops the modern Level 4
  // space-separated form (`hsl(H S% L%)`) that all our tokens are stored
  // in. That dropped style was the root cause of the dark-on-dark label
  // regression and the grey-disc bug — the property never reached the
  // renderer. Normalise here so tokens defined as `H S% L%` work
  // unchanged everywhere else in the codebase.
  const parts = raw.split(/\s+/);
  if (parts.length === 3) {
    return `hsl(${parts[0]}, ${parts[1]}, ${parts[2]})`;
  }
  return `hsl(${raw})`;
}

// ── Public types ──────────────────────────────────────────────────────

export interface NeighborInfo {
  id: string;
  label: string;
  kind: GraphNodeKind;
}

export interface EdgeInfo {
  kind: GraphEdgeKind;
  otherId: string;
  otherKind: GraphNodeKind;
  otherLabel: string;
}

export type NodeClickHandler = (node: {
  id: string;
  label: string;
  kind: GraphNodeKind;
  file: string;
  range: [number, number];
  docstring: string;
  degree: number;
  inEdges: EdgeInfo[];
  outEdges: EdgeInfo[];
  neighbors: NeighborInfo[];
}) => void;

// ── Renderer ──────────────────────────────────────────────────────────

export class KnowledgeGraphRenderer {
  // ── State (atomic attributes) ────────────────────────────────────
  private cy: Core | null = null;
  private readonly container: HTMLElement;
  private nodeClickHandler: NodeClickHandler | null = null;
  private hiddenKinds: Set<GraphNodeKind> = new Set();
  private hiddenEdgeKinds: Set<GraphEdgeKind> = new Set();
  private focusId: string | null = null;
  private textFilter = "";
  private labelThreshold = Number.POSITIVE_INFINITY;

  // ── Construction ─────────────────────────────────────────────────

  constructor(container: HTMLElement) {
    ensureFcose();
    this.container = container;
  }

  // ── Lifecycle ────────────────────────────────────────────────────

  /** Mount the graph for the first time. Idempotent — calling again replaces. */
  render(graph: KnowledgeGraph): void {
    this.dispose();
    const degree = KnowledgeGraphRenderer.degreeMap(graph);
    this.labelThreshold = KnowledgeGraphRenderer.computeLabelThreshold(degree);

    const elements = KnowledgeGraphRenderer.toElements(graph, degree);
    this.cy = cytoscape({
      container: this.container,
      elements,
      style: KnowledgeGraphRenderer.buildStylesheet(),
      layout: KnowledgeGraphRenderer.layoutOptions(graph.nodes.length),
      wheelSensitivity: 0.2,
      minZoom: 0.1,
      maxZoom: 4,
    });
    this.cy.on("tap", "node", this._onNodeTap);
    this.cy.on("tap", this._onBgTap);
    this._classifyAll();

    // Initial-view focus: at fit-all-zoom every disc is sub-pixel for
    // any repo bigger than ~500 nodes, so re-fit to the top-N hubs
    // (most-connected nodes) so the user lands on a structurally
    // meaningful view. The "Fit to view" floating control still
    // restores full-graph view on demand.
    KnowledgeGraphRenderer.fitToHubs(this.cy, degree);
  }

  /** Re-fit the viewport without re-running the layout. */
  fit(): void {
    this.cy?.fit(undefined, 40);
  }

  /** Multiply current zoom by *factor*, animated, capped to {min,max}Zoom. */
  zoomBy(factor: number): void {
    if (!this.cy) return;
    const cy = this.cy;
    const next = Math.max(
      cy.minZoom(),
      Math.min(cy.maxZoom(), cy.zoom() * factor),
    );
    const c = cy.extent();
    cy.animate({
      zoom: { level: next, position: { x: (c.x1 + c.x2) / 2, y: (c.y1 + c.y2) / 2 } },
      duration: 180,
      easing: "ease-out",
    });
  }

  /** Reset focus and re-fit the whole graph. */
  reset(): void {
    this.clearFocus();
    this.cy?.animate({ fit: { eles: this.cy.elements(), padding: 40 }, duration: 220 });
  }

  /** Re-run the force-directed layout. */
  relayout(): void {
    if (!this.cy) return;
    const count = this.cy.nodes().length;
    this.cy.layout(KnowledgeGraphRenderer.layoutOptions(count)).run();
  }

  /** Re-apply themed styles when ``light`` toggles on <html>. */
  applyTheme(): void {
    if (!this.cy) return;
    this.cy.style(KnowledgeGraphRenderer.buildStylesheet()).update();
  }

  /** Destroy the Cytoscape instance and detach listeners. Safe to call twice. */
  dispose(): void {
    if (!this.cy) return;
    this.cy.off("tap", "node", this._onNodeTap);
    this.cy.off("tap", this._onBgTap);
    this.cy.destroy();
    this.cy = null;
    this.focusId = null;
  }

  // ── Filters ──────────────────────────────────────────────────────

  /** Filter nodes by case-insensitive substring on label or file. */
  applyFilter(query: string): void {
    this.textFilter = query.trim().toLowerCase();
    this._classifyAll();
  }

  /** Toggle a whole node-kind on/off. */
  setKindHidden(kind: GraphNodeKind, hidden: boolean): void {
    if (hidden) this.hiddenKinds.add(kind);
    else this.hiddenKinds.delete(kind);
    this._classifyAll();
  }

  /** Toggle a whole edge-kind on/off. */
  setEdgeKindHidden(kind: GraphEdgeKind, hidden: boolean): void {
    if (hidden) this.hiddenEdgeKinds.add(kind);
    else this.hiddenEdgeKinds.delete(kind);
    this._classifyAll();
  }

  // ── Focus mode ───────────────────────────────────────────────────

  /** Pan + zoom so *nodeId* and its 1-hop neighbourhood fill the viewport. */
  focusNode(nodeId: string): void {
    if (!this.cy) return;
    const node = this.cy.getElementById(nodeId);
    if (node.empty()) return;
    this.focusId = nodeId;
    this._classifyAll();
    const neighbourhood = node.closedNeighborhood();
    this.cy.animate({
      fit: { eles: neighbourhood, padding: 80 },
      duration: 360,
      easing: "ease-in-out",
    });
  }

  /** Release focus and restore full-graph view. */
  clearFocus(): void {
    if (!this.focusId) return;
    this.focusId = null;
    this._classifyAll();
  }

  isFocused(): boolean {
    return this.focusId !== null;
  }

  // ── Listeners ────────────────────────────────────────────────────

  /** Register the node-click callback. Last registration wins. */
  onNodeClick(handler: NodeClickHandler | null): void {
    this.nodeClickHandler = handler;
  }

  // ── Private handlers ─────────────────────────────────────────────

  private readonly _onBgTap = (evt: EventObject): void => {
    if (!this.cy) return;
    if (evt.target === this.cy && this.focusId) this.clearFocus();
  };

  private readonly _onNodeTap = (evt: EventObject): void => {
    if (!this.cy) return;
    const node = evt.target;
    const d = node.data();
    const id = String(d.id ?? "");
    this.focusNode(id);

    if (!this.nodeClickHandler) return;
    const cy = this.cy;

    const fmt = (e: cytoscape.EdgeSingular, otherDir: "source" | "target"): EdgeInfo => {
      const other = cy.getElementById(e.data(otherDir) as string);
      const od = other.data();
      return {
        kind: e.data("kind") as GraphEdgeKind,
        otherId: String(od?.id ?? ""),
        otherKind: (od?.kind as GraphNodeKind) ?? "File",
        otherLabel: String(od?.label ?? ""),
      };
    };

    const inList: EdgeInfo[] = [];
    node.incomers("edge").forEach((e: cytoscape.EdgeSingular) => {
      inList.push(fmt(e, "source"));
    });
    const outList: EdgeInfo[] = [];
    node.outgoers("edge").forEach((e: cytoscape.EdgeSingular) => {
      outList.push(fmt(e, "target"));
    });
    const neighbors: NeighborInfo[] = [];
    node.openNeighborhood("node").forEach((n: cytoscape.NodeSingular) => {
      neighbors.push({
        id: String(n.data("id") ?? ""),
        kind: (n.data("kind") as GraphNodeKind) ?? "File",
        label: String(n.data("label") ?? ""),
      });
    });

    this.nodeClickHandler({
      id,
      label: String(d.label ?? ""),
      kind: (d.kind as GraphNodeKind) ?? "File",
      file: String(d.file ?? ""),
      range: (d.range as [number, number]) ?? [0, 0],
      docstring: String(d.docstring ?? ""),
      degree: (d.degree as number) ?? 0,
      inEdges: inList,
      outEdges: outList,
      neighbors,
    });
  };

  /** Re-classify every node/edge given the current filter + focus state. */
  private _classifyAll(): void {
    if (!this.cy) return;
    const cy = this.cy;
    const focusId = this.focusId;
    const focused = focusId ? cy.getElementById(focusId) : null;

    const neighbourSet = new Set<string>();
    if (focused && !focused.empty() && focusId) {
      neighbourSet.add(focusId);
      focused.openNeighborhood("node").forEach((n: cytoscape.NodeSingular) => {
        neighbourSet.add(String(n.data("id")));
      });
    }

    cy.batch(() => {
      cy.nodes().forEach((n) => {
        const id = String(n.data("id"));
        const kind = (n.data("kind") as GraphNodeKind) ?? "File";
        const label = String(n.data("label") ?? "").toLowerCase();
        const file = String(n.data("file") ?? "").toLowerCase();
        const degree = (n.data("degree") as number) ?? 0;

        const kindHidden = this.hiddenKinds.has(kind);
        const textHidden =
          this.textFilter !== "" &&
          !label.includes(this.textFilter) &&
          !file.includes(this.textFilter);
        n.toggleClass("hidden", kindHidden || textHidden);

        if (focusId) {
          const isFocus = id === focusId;
          const isNeighbour = neighbourSet.has(id);
          n.toggleClass("focused", isFocus);
          n.toggleClass("neighbor", isNeighbour && !isFocus);
          n.toggleClass("dimmed", !isNeighbour);
        } else {
          n.removeClass("focused neighbor dimmed");
        }

        n.toggleClass("labelled", degree >= this.labelThreshold);
      });

      cy.edges().forEach((e) => {
        const src = cy.getElementById(e.data("source") as string);
        const tgt = cy.getElementById(e.data("target") as string);
        const ekind = e.data("kind") as GraphEdgeKind;
        const kindHidden = this.hiddenEdgeKinds.has(ekind);
        const endpointHidden = src.hasClass("hidden") || tgt.hasClass("hidden");
        e.toggleClass("hidden", kindHidden || endpointHidden);

        if (focusId) {
          const inFocus =
            neighbourSet.has(String(src.data("id"))) &&
            neighbourSet.has(String(tgt.data("id")));
          e.toggleClass("dimmed", !inFocus);
        } else {
          e.removeClass("dimmed");
        }
      });
    });
  }

  // ── Static helpers (configuration over state) ─────────────────────

  /** Position the initial viewport so discs read at a usable size.
   *
   *  Fit-all on a multi-thousand-node repo zooms each disc to sub-pixel
   *  size — every node looks the same, the user can't see structure.
   *  Instead we land on a fixed zoom level where labelled hubs render
   *  readably (~14-30 px discs), centred on the graph's mass-centroid.
   *  The "Fit to view" floating control still restores full-graph view
   *  for users who want the bird's-eye picture. */
  static fitToHubs(cy: Core, degree: Map<string, number>): void {
    const total = degree.size;
    if (total <= 250) {
      cy.fit(undefined, 60);
      return;
    }

    // Centroid weighted by degree — biases the camera toward the
    // structurally important region instead of an empty quadrant.
    let sumX = 0;
    let sumY = 0;
    let totalWeight = 0;
    cy.nodes().forEach((n) => {
      const w = ((n.data("degree") as number) ?? 0) + 1;
      const p = n.position();
      sumX += p.x * w;
      sumY += p.y * w;
      totalWeight += w;
    });
    const cx = totalWeight > 0 ? sumX / totalWeight : 0;
    const cy0 = totalWeight > 0 ? sumY / totalWeight : 0;

    // Target zoom that keeps a hub disc (~50 px) at ~25 px on screen —
    // small enough to fit a meaningful chunk, large enough that the
    // labelled hubs and their colour read clearly.
    const targetZoom = total > 2000 ? 0.5 : total > 800 ? 0.7 : 0.9;
    const cont = cy.container();
    if (!cont) {
      cy.fit(undefined, 60);
      return;
    }
    // Atomic viewport set: pan so graph-coord (cx, cy0) lands at the
    // container's pixel centre, at the chosen zoom.
    cy.viewport({
      zoom: targetZoom,
      pan: {
        x: cont.clientWidth / 2 - cx * targetZoom,
        y: cont.clientHeight / 2 - cy0 * targetZoom,
      },
    });
  }

  /** Sum in + out degree per node id from the wire graph. */
  static degreeMap(graph: KnowledgeGraph): Map<string, number> {
    const m = new Map<string, number>();
    for (const n of graph.nodes) m.set(n.data.id, 0);
    for (const e of graph.edges) {
      m.set(e.data.source, (m.get(e.data.source) ?? 0) + 1);
      m.set(e.data.target, (m.get(e.data.target) ?? 0) + 1);
    }
    return m;
  }

  /** Degree at the 80th percentile — labels above this stay always-on. */
  static computeLabelThreshold(degree: Map<string, number>): number {
    if (degree.size === 0) return Number.POSITIVE_INFINITY;
    const sorted = [...degree.values()].sort((a, b) => b - a);
    const idx = Math.max(0, Math.floor(sorted.length * 0.2) - 1);
    return sorted[idx] ?? 0;
  }

  /** Map the wire format to a Cytoscape elements array, decorated with degree. */
  static toElements(
    graph: KnowledgeGraph,
    degree: Map<string, number>,
  ): ElementDefinition[] {
    const nodes = graph.nodes.map((n) => ({
      data: { ...n.data, degree: degree.get(n.data.id) ?? 0 },
    }));
    return [...nodes, ...graph.edges] as unknown as ElementDefinition[];
  }

  /** Stylesheet — colours resolved once from CSS vars, then used as static
   *  per-kind selector overlays. Cytoscape doesn't resolve ``var(--…)`` on
   *  its <canvas>, so the var → hex conversion has to happen here. */
  static buildStylesheet(): cytoscape.StylesheetJson {
    const sizeFor = (n: cytoscape.NodeSingular): number => {
      const d = (n.data("degree") as number) ?? 0;
      return Math.min(60, 28 + 6 * Math.log2(1 + d));
    };

    const fg = cssVar("--foreground");
    const bg = cssVar("--background");
    const primary = cssVar("--primary");

    // Per-kind overlays — static colour + icon URI.
    const kindOverlays = (Object.keys(KIND_VAR) as GraphNodeKind[]).map(
      (kind) => ({
        selector: `node[kind = "${kind}"]`,
        style: {
          "background-color": cssVar(KIND_VAR[kind]),
          "background-image": ICON_URI[kind],
        },
      }),
    );
    const edgeOverlays = (Object.keys(EDGE_VAR) as GraphEdgeKind[]).map(
      (kind) => ({
        selector: `edge[kind = "${kind}"]`,
        style: {
          "line-color": cssVar(EDGE_VAR[kind]),
          "target-arrow-color": cssVar(EDGE_VAR[kind]),
        },
      }),
    );

    return [
      {
        selector: "node",
        style: {
          "background-fit": "none",
          "background-width": "60%",
          "background-height": "60%",
          "background-image-opacity": 1,
          width: sizeFor,
          height: sizeFor,
          "border-width": 1.5,
          "border-color": bg,
          "border-opacity": 0.85,
          label: "data(label)",
          color: fg,
          "font-size": 10,
          "font-weight": 500,
          "text-margin-y": 6,
          "text-valign": "bottom",
          "text-halign": "center",
          "text-opacity": 0,
          "text-background-color": bg,
          "text-background-opacity": 0.85,
          "text-background-padding": "2px",
          "text-background-shape": "roundrectangle",
        },
      },
      ...kindOverlays,
      {
        selector: "node.labelled, node:selected, node.focused, node.neighbor",
        style: { "text-opacity": 1 },
      },
      {
        selector: "node.focused",
        style: {
          "border-color": primary,
          "border-width": 3,
          "border-opacity": 1,
        },
      },
      {
        selector: "node.dimmed",
        style: {
          "background-opacity": 0.18,
          "background-image-opacity": 0.18,
          "border-opacity": 0.08,
          "text-opacity": 0,
        },
      },
      {
        selector: "edge",
        style: {
          width: 1,
          "curve-style": "bezier",
          "line-opacity": 0.5,
          "target-arrow-shape": "triangle",
          "arrow-scale": 0.7,
        },
      },
      ...edgeOverlays,
      {
        selector: "edge.dimmed",
        style: { "line-opacity": 0.06, "target-arrow-shape": "none" },
      },
      {
        selector: ".hidden",
        style: { display: "none" },
      },
    ] as unknown as cytoscape.StylesheetJson;
  }

  /** Layout config — fcose tuned for the Neo4j-Bloom-lite disc size. */
  static layoutOptions(nodeCount: number): cytoscape.LayoutOptions {
    return {
      name: "fcose",
      animate: false,
      randomize: true,
      // Quality knob — "proof" runs the longer optimisation pass and is
      // the difference between a "colourful pile" and a readable graph
      // for ~thousands of nodes. Worth the extra ~3-5 s on first render.
      quality: "proof",
      // Account for the disc width (28-60 px) AND the label pill so the
      // layout never overlaps a labelled hub with another node.
      nodeDimensionsIncludeLabels: true,
      uniformNodeDimensions: false,
      // Spacing knobs tuned for the 4 K-node real-repo case. Repulsion is
      // the dominant lever — pushing it well past the fcose default
      // (4500) is what spreads dense neighbourhoods. Edge length keeps
      // 1-hop neighbours readable instead of stacked.
      idealEdgeLength: nodeCount > 2000 ? 220 : nodeCount > 400 ? 140 : 90,
      nodeRepulsion: nodeCount > 2000 ? 60000 : nodeCount > 400 ? 25000 : 15000,
      nodeSeparation: 120,
      // Lower gravity = less pull to centre = more spread across the
      // viewport. Range high so disconnected components drift apart.
      gravity: nodeCount > 2000 ? 0.05 : 0.15,
      gravityRange: 4.0,
      // Pack disconnected components side-by-side rather than stacking.
      packComponents: true,
      tile: true,
      tilingPaddingVertical: 24,
      tilingPaddingHorizontal: 24,
      numIter: nodeCount > 2000 ? 3500 : 2000,
      // After layout finishes, fit the viewport to all elements with a
      // small inset so users land on a usable view.
      fit: true,
      padding: 50,
    } as unknown as cytoscape.LayoutOptions;
  }
}
