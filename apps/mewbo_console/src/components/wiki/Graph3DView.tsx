/**
 * Graph3DView — the ONE 3D "Code Galaxy" graph renderer, shared by every graph
 * surface in the console (the wiki Knowledge Graph AND the Agentic-Search SCG
 * workspace graph). It replaces the old 2D Cytoscape ``KnowledgeGraphRenderer``
 * that both surfaces forked — a main-thread fcose force-sim that choked past a
 * few thousand nodes. This is the GPU/WebGL path (``react-force-graph-3d`` /
 * three.js) and the SINGLE implementation: one engine, one toolbar grammar, one
 * collapse model — no per-domain renderer.
 *
 * It is deliberately THIN glue around the off-the-shelf ``<ForceGraph3D>`` (it
 * owns the three.js scene, camera, picking, force layout, hover labels and nav)
 * plus the pure, unit-tested ``CollapseModel`` (visible-set + edge aggregation
 * over a directory hierarchy). The component is DOMAIN-AGNOSTIC: it treats node
 * ``kind`` / ``layer`` as opaque strings and reads every colour / label / layer
 * from the injected {@link Graph3DTheme}; the caller renders the side-panel via
 * the {@link Graph3DViewProps.renderInspector} render-prop. The wiki and SCG
 * both satisfy the structural ``{ nodes:[{data}], edges:[{data}] }`` wire shape,
 * so each is a thin adapter supplying a theme + an inspector.
 *
 * A graph WITHOUT a folder hierarchy (the SCG case — no ``Folder`` nodes, no
 * ``CONTAINS`` edges) flows through ``CollapseModel`` as a pure pass-through, so
 * there is no branch for "hierarchy vs flat" — KISS.
 */
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import ForceGraph3D, { type ForceGraphMethods } from "react-force-graph-3d";
import { Loader2, Maximize2, RotateCcw, Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { CollapseModel } from "./collapseModel";
import { cssVarColor, cssVarColorAlpha, nodeSize } from "./graphTheme";
import type { KnowledgeGraph, KnowledgeGraphEdge } from "./api/types";

// ── Structural wire shapes (both KnowledgeGraph + WorkspaceGraph satisfy them) ──

/** The node ``data`` fields this view reads. ``kind`` / ``layer`` are opaque
 *  strings; domain-specific extras (``doc`` / ``snippet`` / ``file`` …) ride
 *  along at runtime and are read by the caller's inspector, not here. */
export interface Graph3DNodeData {
  id: string;
  label: string;
  kind: string;
  layer?: string;
  /** AST/code graphs — the file a symbol lives in (hover label). */
  file?: string;
  /** Hierarchy scaffold — the directory path of a ``Folder`` supernode. */
  folderPath?: string | null;
  /** Hierarchy scaffold — id of the enclosing ``Folder`` (or null = root). */
  parentId?: string | null;
}

export interface Graph3DEdgeData {
  id: string;
  source: string;
  target: string;
  kind: string;
  weight?: number;
  label?: string;
}

export interface Graph3DWire {
  nodes: Array<{ data: Graph3DNodeData }>;
  edges: Array<{ data: Graph3DEdgeData }>;
}

/** A node datum fed to the force-graph: the wire node's ``data`` flattened (the
 *  engine keys on ``id``) with a derived ``degree`` for sizing. The engine
 *  mutates ``x/y/z`` in place once it lays the node out. Domain-specific fields
 *  survive on the runtime object — the inspector casts to read them. */
export type Graph3DNode = Graph3DNodeData & {
  degree: number;
  x?: number;
  y?: number;
  z?: number;
};

/** A link datum — the collapsed edge's ``data``. ``source``/``target`` start as
 *  ids and the engine swaps them for node refs once linked. */
export interface Graph3DLink {
  id: string;
  source: string | Graph3DNode;
  target: string | Graph3DNode;
  kind: string;
  label?: string;
  weight?: number;
  /** Constituent originals folded into an aggregated supernode↔supernode tie. */
  aggregated?: KnowledgeGraphEdge[];
  /** Faint hierarchy-backbone tie (re-pointed ``CONTAINS``). */
  containment?: boolean;
}

/** What a click selects — a node OR a link (never both). */
export interface Graph3DPick {
  node: Graph3DNode | null;
  link: Graph3DLink | null;
}

/** Context handed to the inspector render-prop. */
export interface Graph3DInspectorCtx {
  graph: Graph3DWire;
  /** Camera-focus a node by id (drives the panel's "go to" links). */
  onNavigate: (id: string) => void;
  /** Clear the selection (dismiss the inspector). */
  onClose: () => void;
}

/** The injected per-domain visual vocabulary. Every map is keyed by the closed
 *  kind/layer unions of the DOMAIN (wiki or SCG) so each is exhaustive where it
 *  is defined; this view consumes them as ``Record<string, …>``. */
export interface Graph3DTheme {
  /** All node kinds, in toolbar order. */
  allKinds: string[];
  /** ``--graph-*`` CSS-var name for the node fill, per kind. */
  kindVar: Record<string, string>;
  /** Tailwind ``bg-…`` class for the toolbar legend dot, per kind. */
  kindDot: Record<string, string>;
  /** Human label for a kind chip. */
  kindLabel: Record<string, string>;
  /** ``--graph-*`` CSS-var name for the edge line, per edge kind. */
  edgeVar: Record<string, string>;
  /** The multiplex layer a kind belongs to (drives the per-layer toggle). */
  kindLayer: Record<string, string>;
  /** Layers in toolbar order. */
  layerOrder: string[];
  layerLabel: Record<string, string>;
  layerDot: Record<string, string>;
  /** Folder supernode fill token (hierarchy graphs only). */
  folderVar?: string;
  /** Faint containment-backbone token (hierarchy graphs only). */
  edgeContainVar?: string;
  /** Optional rich hover label; default = ``node.label``. */
  nodeLabel?: (node: Graph3DNode, foldedCount: number) => string;
}

/** Pre-aggregated counts the toolbar reads (each domain maps its wire stats). */
export interface Graph3DStats {
  nodeCount: number;
  edgeCount: number;
  /** Count of synthetic ``Folder`` supernodes (hierarchy graphs only). */
  folderCount?: number;
  /** Per-kind tallies — the SINGLE source for both the kind chips AND the
   *  per-layer badges (the layer count is summed from its kinds via
   *  ``theme.kindLayer``). Deriving the layer count this way — rather than a
   *  separate server ``perLayer`` stat — guarantees the badge always equals
   *  what the layer toggle actually hides/shows. */
  kinds: Partial<Record<string, number>>;
}

export interface Graph3DViewProps {
  /** The wire graph; ``null`` while loading. */
  graph: Graph3DWire | null;
  /** Pre-aggregated stats for the toolbar; ``null`` while loading. */
  stats: Graph3DStats | null;
  isPending: boolean;
  isError: boolean;
  theme: Graph3DTheme;
  /** Render the domain side-panel for the current pick. */
  renderInspector?: (pick: Graph3DPick, ctx: Graph3DInspectorCtx) => ReactNode;
  /** A floating banner over the canvas (e.g. the SCG "all unmapped" hint). */
  banner?: ReactNode;
  /** Trailing toolbar hint (e.g. "Click a folder to expand · drag to orbit"). */
  hint?: string;
  /** Loading / error / empty copy (sensible defaults below). */
  labels?: { loading?: string; error?: string; empty?: string };
}

/** A force-graph endpoint is an id before linking, the resolved node after. */
function endId(end: string | Graph3DNode): string {
  return typeof end === "string" ? end : end.id;
}

export function Graph3DView({
  graph,
  stats,
  isPending,
  isError,
  theme,
  renderInspector,
  banner,
  hint,
  labels,
}: Graph3DViewProps) {
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<Graph3DNode, Graph3DLink> | undefined>(undefined);
  const [pick, setPick] = useState<Graph3DPick>({ node: null, link: null });
  const [filter, setFilter] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  // The ONE atomic class — pure collapse logic, rebuilt only when the graph
  // identity changes. A folderless graph (SCG) passes straight through. The
  // cast is the same structural-compat seam both surfaces already used to feed
  // the old shared renderer; CollapseModel reads only id/parentId/kind/endpoints.
  const model = useMemo(
    () => (graph ? new CollapseModel(graph as unknown as KnowledgeGraph) : null),
    [graph],
  );
  const folded = useMemo(() => model?.foldedCounts() ?? new Map<string, number>(), [model]);
  const degree = useMemo(() => {
    const m = new Map<string, number>();
    if (!graph) return m;
    for (const n of graph.nodes) m.set(n.data.id, 0);
    for (const e of graph.edges) {
      if (e.data.kind === "CONTAINS") continue;
      m.set(e.data.source, (m.get(e.data.source) ?? 0) + 1);
      m.set(e.data.target, (m.get(e.data.target) ?? 0) + 1);
    }
    return m;
  }, [graph]);

  // Seed expansion to depth-1 folders whenever a fresh graph loads (a no-op
  // empty set for folderless graphs).
  useEffect(() => {
    if (graph) setExpanded(CollapseModel.initialExpanded(graph as unknown as KnowledgeGraph));
    setPick({ node: null, link: null });
  }, [graph]);

  // Derive the renderable graph straight from the model + expansion state — a
  // memoised pure transform fed to the component.
  const graphData = useMemo<{ nodes: Graph3DNode[]; links: Graph3DLink[] }>(() => {
    if (!model) return { nodes: [], links: [] };
    const v = model.visibleGraph(expanded);
    return {
      nodes: v.nodes.map(
        (n): Graph3DNode => ({ ...(n.data as Graph3DNodeData), degree: degree.get(n.data.id) ?? 0 }),
      ),
      links: v.edges.map((e) => ({ ...e.data })) as Graph3DLink[],
    };
  }, [model, expanded, degree]);

  // id → kind, for O(1) per-frame link visibility (never scan the node array).
  const kindById = useMemo(() => {
    const m = new Map<string, string>();
    for (const n of graphData.nodes) m.set(n.id, n.kind);
    return m;
  }, [graphData]);

  // Track the canvas box so <ForceGraph3D> fills the pane (it defaults to the
  // window otherwise). ResizeObserver is the right primitive.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  // ── Interaction ──────────────────────────────────────────────────
  const clearPick = (): void => setPick({ node: null, link: null });

  const toggleFolder = (id: string): void => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    fgRef.current?.d3ReheatSimulation();
  };

  const handleNodeClick = (n: Graph3DNode): void => {
    if (n.kind === "Folder") toggleFolder(n.id);
    setPick({ node: n, link: null });
  };

  const handleLinkClick = (l: Graph3DLink): void => setPick({ node: null, link: l });

  const focusBy = (id: string): void => {
    const fg = fgRef.current;
    if (!fg) return;
    const node = graphData.nodes.find((n) => n.id === id);
    if (!node || node.x == null) return;
    const x = node.x;
    const y = node.y ?? 0;
    const z = node.z ?? 0;
    const ratio = 1 + 120 / (Math.hypot(x, y, z) || 1);
    fg.cameraPosition({ x: x * ratio, y: y * ratio, z: z * ratio }, { x, y, z }, 800);
  };

  // ── Toolbar derivations ──────────────────────────────────────────
  const kindCounts = useMemo(() => stats?.kinds ?? {}, [stats]);
  const visibleKinds = useMemo(
    () => theme.allKinds.filter((k) => (kindCounts[k] ?? 0) > 0),
    [theme.allKinds, kindCounts],
  );
  const kindsForLayer = (layer: string): string[] =>
    theme.allKinds.filter((k) => theme.kindLayer[k] === layer);
  // Layer counts derive from the per-kind tallies (sum over the layer's kinds)
  // so a layer-only count never needs a separate stat — this also folds the
  // SCG "unmapped ghost rides the schema layer" rule in for free.
  const layerCounts = useMemo(() => {
    const out: Record<string, number> = {};
    for (const layer of theme.layerOrder) {
      out[layer] = kindsForLayer(layer).reduce((sum, k) => sum + (kindCounts[k] ?? 0), 0);
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, kindCounts]);
  const presentLayers = useMemo(
    () => theme.layerOrder.filter((l) => (layerCounts[l] ?? 0) > 0),
    [theme.layerOrder, layerCounts],
  );

  const toggleKind = (k: string): void =>
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });

  const layerShown = (layer: string): boolean =>
    kindsForLayer(layer).some((k) => !hiddenKinds.has(k));

  const toggleLayer = (layer: string): void => {
    const kinds = kindsForLayer(layer);
    const hide = layerShown(layer);
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      for (const k of kinds) {
        if (hide) next.add(k);
        else next.delete(k);
      }
      return next;
    });
  };

  const filterLc = filter.trim().toLowerCase();
  const isFiltered = (n: Graph3DNode): boolean =>
    filterLc !== "" &&
    !n.label.toLowerCase().includes(filterLc) &&
    !(n.file ?? "").toLowerCase().includes(filterLc);

  const resetView = (): void => {
    if (graph) setExpanded(CollapseModel.initialExpanded(graph as unknown as KnowledgeGraph));
    clearPick();
    fgRef.current?.zoomToFit(600, 60);
  };

  const isEmpty = !isPending && !isError && (graph?.nodes.length ?? 0) === 0;
  const hasPick = pick.node !== null || pick.link !== null;

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* Toolbar */}
      <div className="border-b border-[hsl(var(--border))] px-4 sm:px-6 py-2 flex items-center gap-3 flex-wrap">
        <div className="relative w-full max-w-[280px]">
          <Search className="h-3.5 w-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
          <input
            type="search"
            placeholder="Filter nodes by name…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="w-full pl-7 pr-8 h-8 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-xs placeholder:text-[hsl(var(--muted-foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/40"
          />
          {filter && (
            <button
              type="button"
              onClick={() => setFilter("")}
              aria-label="Clear filter"
              className="absolute right-1 top-1/2 -translate-y-1/2 p-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
            >
              <X className="h-3 w-3" />
            </button>
          )}
        </div>

        {stats && (
          <div className="flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
            <span className="mr-1">
              <span className="font-mono text-[hsl(var(--foreground))]">{stats.nodeCount}</span>{" "}
              nodes
              <span className="mx-1.5 opacity-30">·</span>
              <span className="font-mono text-[hsl(var(--foreground))]">{stats.edgeCount}</span>{" "}
              edges
              {typeof stats.folderCount === "number" && (
                <>
                  <span className="mx-1.5 opacity-30">·</span>
                  <span className="font-mono text-[hsl(var(--foreground))]">
                    {stats.folderCount}
                  </span>{" "}
                  folders
                </>
              )}
            </span>
            <span className="opacity-30">|</span>
            <Popover>
              <PopoverTrigger asChild>
                <button
                  type="button"
                  className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[11px] hover:bg-[hsl(var(--muted))]/40"
                >
                  Node types
                </button>
              </PopoverTrigger>
              <PopoverContent align="start" className="w-56 p-2">
                <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] px-1 pb-1">
                  Node types
                </div>
                <div className="flex flex-col">
                  {visibleKinds.map((k) => {
                    const isHidden = hiddenKinds.has(k);
                    return (
                      <button
                        key={k}
                        type="button"
                        onClick={() => toggleKind(k)}
                        aria-pressed={!isHidden}
                        className={cn(
                          "flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-left",
                          "hover:bg-[hsl(var(--muted))]/40",
                          isHidden && "opacity-40 line-through",
                        )}
                      >
                        <span className={cn("w-2 h-2 rounded-full", theme.kindDot[k])} />
                        <span className="text-[hsl(var(--foreground))] flex-1">
                          {theme.kindLabel[k] ?? k}
                        </span>
                        <span className="font-mono text-[hsl(var(--muted-foreground))]">
                          {kindCounts[k]}
                        </span>
                      </button>
                    );
                  })}
                </div>
              </PopoverContent>
            </Popover>

            {presentLayers.length > 1 && (
              <>
                <span className="opacity-30">|</span>
                <div
                  role="group"
                  aria-label="Toggle graph layers"
                  className="inline-flex items-center rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden"
                >
                  {presentLayers.map((layer, i) => {
                    const shown = layerShown(layer);
                    return (
                      <Button
                        key={layer}
                        variant="ghost"
                        size="sm"
                        onClick={() => toggleLayer(layer)}
                        aria-pressed={shown}
                        title={
                          shown
                            ? `Hide ${theme.layerLabel[layer]} layer`
                            : `Show ${theme.layerLabel[layer]} layer`
                        }
                        className={cn(
                          "h-6 gap-1.5 px-2.5 rounded-none text-[11px]",
                          i > 0 && "border-l border-[hsl(var(--border))]",
                          shown
                            ? "bg-[hsl(var(--muted))]/40 text-[hsl(var(--foreground))]"
                            : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]/20",
                        )}
                      >
                        <span
                          className={cn(
                            "w-2 h-2 rounded-full",
                            theme.layerDot[layer],
                            !shown && "opacity-40",
                          )}
                        />
                        <span>{theme.layerLabel[layer]}</span>
                        <span className="font-mono text-[hsl(var(--muted-foreground))]">
                          {layerCounts[layer]}
                        </span>
                      </Button>
                    );
                  })}
                </div>
              </>
            )}
          </div>
        )}

        <div className="flex-1" />
        {hint && (
          <span className="text-[11px] text-[hsl(var(--muted-foreground))] hidden sm:inline">
            {hint}
          </span>
        )}
      </div>

      {/* Canvas + inspector */}
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 relative">
          <div ref={canvasRef} className="absolute inset-0 bg-[hsl(var(--background))]">
            {size.w > 0 && size.h > 0 && (
              <ForceGraph3D
                ref={fgRef}
                width={size.w}
                height={size.h}
                graphData={graphData}
                showNavInfo={false}
                backgroundColor="rgba(0,0,0,0)"
                nodeRelSize={4}
                nodeColor={(n) =>
                  isFiltered(n)
                    ? cssVarColor("--muted-foreground")
                    : cssVarColor(
                        n.kind === "Folder" && theme.folderVar
                          ? theme.folderVar
                          : theme.kindVar[n.kind] ?? "--graph-edge-soft",
                      )
                }
                nodeVal={(n) => nodeSize(n.degree, n.kind === "Folder", folded.get(n.id) ?? 0)}
                nodeVisibility={(n) => !hiddenKinds.has(n.kind)}
                nodeLabel={(n) =>
                  theme.nodeLabel ? theme.nodeLabel(n, folded.get(n.id) ?? 0) : n.label
                }
                nodeOpacity={0.95}
                linkColor={(l) =>
                  l.containment
                    ? // The 3D renderer multiplies the global linkOpacity (0.35)
                      // by the colour's alpha, so baking ~0.4 here lands the
                      // backbone at ~0.14 — clearly under the relationship edges.
                      cssVarColorAlpha(theme.edgeContainVar ?? "--graph-edge-contain", 0.4)
                    : cssVarColor(theme.edgeVar[l.kind] ?? "--graph-edge-soft")
                }
                linkWidth={(l) =>
                  l.containment ? 0.3 : Math.min(4, 0.5 + Math.log2(1 + (l.weight ?? 1)))
                }
                linkVisibility={(l) => {
                  const s = kindById.get(endId(l.source));
                  const t = kindById.get(endId(l.target));
                  return !((s && hiddenKinds.has(s)) || (t && hiddenKinds.has(t)));
                }}
                linkOpacity={0.35}
                linkDirectionalArrowLength={(l) => (l.containment ? 0 : 2.5)}
                linkDirectionalArrowRelPos={1}
                enableNodeDrag={false}
                cooldownTicks={120}
                onNodeClick={handleNodeClick}
                onLinkClick={handleLinkClick}
                onBackgroundClick={clearPick}
              />
            )}
          </div>

          {banner && (
            <div className="absolute inset-x-0 top-3 z-10 flex justify-center px-6">{banner}</div>
          )}

          {/* Floating fit / reset controls. */}
          <div className="absolute bottom-3 right-3 z-10 flex flex-col rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-md overflow-hidden">
            <button
              type="button"
              onClick={() => fgRef.current?.zoomToFit(600, 60)}
              aria-label="Fit to view"
              title="Fit to view"
              className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50"
            >
              <Maximize2 className="h-4 w-4" />
            </button>
            <button
              type="button"
              onClick={resetView}
              aria-label="Reset view"
              title="Reset view (collapse to top level, fit all)"
              className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]"
            >
              <RotateCcw className="h-4 w-4" />
            </button>
          </div>

          {isPending && (
            <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))]">
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              <span className="text-xs">{labels?.loading ?? "Loading graph…"}</span>
            </div>
          )}
          {isError && (
            <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-xs">
              {labels?.error ?? "Graph unavailable."}
            </div>
          )}
          {isEmpty && (
            <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-xs">
              {labels?.empty ?? "No graph data yet."}
            </div>
          )}
        </div>

        {hasPick &&
          renderInspector?.(pick, {
            graph: graph as Graph3DWire,
            onNavigate: focusBy,
            onClose: clearPick,
          })}
      </div>
    </div>
  );
}
