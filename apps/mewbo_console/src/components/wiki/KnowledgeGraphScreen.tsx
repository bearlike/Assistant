/**
 * Knowledge-graph viewer screen — ``/wiki/graph?slug=…&platform=…``.
 *
 * Wraps a single ``KnowledgeGraphRenderer`` (one Cytoscape canvas) with the
 * shared WikiTopBar, a search box, kind/edge filter chips, a stats strip,
 * and a node-detail side panel. All visualisation logic lives in the
 * renderer; this component owns the React lifecycle around it.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import {
  ChevronDown,
  Loader2,
  Maximize2,
  RotateCcw,
  Search,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { WikiTopBar } from "./WikiTopBar";
import { useKnowledgeGraph } from "./api/hooks";
import type { GraphEdgeKind, GraphLayer, GraphNodeKind } from "./api/types";
import {
  KnowledgeGraphRenderer,
  type EdgeInfo,
  type NeighborInfo,
  type NodeClickHandler,
} from "./KnowledgeGraphRenderer";
import type { PlatformId } from "./router";

interface KnowledgeGraphScreenProps {
  slug?: string;
  platform?: PlatformId;
}

interface SelectedNode {
  id: string;
  label: string;
  kind: GraphNodeKind;
  layer: GraphLayer;
  file: string;
  range: [number, number];
  docstring: string;
  entityType?: string;
  labels?: string[];
  snippet?: string;
  degree: number;
  inEdges: EdgeInfo[];
  outEdges: EdgeInfo[];
  neighbors: NeighborInfo[];
}

const ALL_NODE_KINDS: GraphNodeKind[] = [
  "File",
  "Module",
  "Class",
  "Function",
  "Method",
  "Interface",
  "External",
  "Entity",
  "Memory",
];

const ALL_EDGE_KINDS: GraphEdgeKind[] = [
  "CONTAINS",
  "IMPORTS",
  "CALLS",
  "EXTENDS",
  "REFERENCES",
  "ANCHORS",
  "RELATES",
];

const KIND_DOT: Record<GraphNodeKind, string> = {
  File: "bg-[hsl(var(--graph-file))]",
  Module: "bg-[hsl(var(--graph-module))]",
  Class: "bg-[hsl(var(--graph-class))]",
  Function: "bg-[hsl(var(--graph-function))]",
  Method: "bg-[hsl(var(--graph-method))]",
  Interface: "bg-[hsl(var(--graph-interface))]",
  External: "bg-[hsl(var(--graph-external))]",
  Entity: "bg-[hsl(var(--graph-entity))]",
  Memory: "bg-[hsl(var(--graph-memory))]",
};

const EDGE_DOT: Record<GraphEdgeKind, string> = {
  CONTAINS: "bg-[hsl(var(--graph-edge-soft))]",
  IMPORTS: "bg-[hsl(var(--graph-file))]",
  CALLS: "bg-[hsl(var(--graph-function))]",
  EXTENDS: "bg-[hsl(var(--graph-class))]",
  REFERENCES: "bg-[hsl(var(--graph-edge-soft))]",
  ANCHORS: "bg-[hsl(var(--graph-edge-anchor))]",
  RELATES: "bg-[hsl(var(--graph-edge-relates))]",
};

// Per-layer segmented toggle config. Bulk-setting a layer's node kinds in
// ``hiddenKinds`` cascades to its edges (endpoint-hidden rule in the
// renderer), so the toggle never enumerates edge kinds — a thin wrapper
// over the existing kind machinery.
const LAYER_ORDER: GraphLayer[] = ["ast", "entity", "memory"];

const LAYER_LABEL: Record<GraphLayer, string> = {
  ast: "Code",
  entity: "Entities",
  memory: "Memory",
};

const LAYER_DOT: Record<GraphLayer, string> = {
  ast: "bg-[hsl(var(--graph-file))]",
  entity: "bg-[hsl(var(--graph-entity))]",
  memory: "bg-[hsl(var(--graph-memory))]",
};

export function KnowledgeGraphScreen({ slug }: KnowledgeGraphScreenProps) {
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<KnowledgeGraphRenderer | null>(null);
  const [selected, setSelected] = useState<SelectedNode | null>(null);
  const [filter, setFilter] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<GraphNodeKind>>(new Set());
  const [hiddenEdgeKinds, setHiddenEdgeKinds] = useState<Set<GraphEdgeKind>>(
    new Set(),
  );
  const [focused, setFocused] = useState(false);

  const query = useKnowledgeGraph(slug ?? null);

  // Boot the renderer once the canvas div is attached.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || rendererRef.current) return;
    rendererRef.current = new KnowledgeGraphRenderer(el);
    return () => {
      rendererRef.current?.dispose();
      rendererRef.current = null;
    };
  }, []);

  // Render data when it arrives. Re-renders when the slug changes.
  useEffect(() => {
    if (!query.data || !rendererRef.current) return;
    rendererRef.current.render(query.data);
    rendererRef.current.onNodeClick(((node) => {
      setSelected(node);
      setFocused(true);
    }) as NodeClickHandler);
  }, [query.data]);

  // Push filter changes to the renderer.
  useEffect(() => {
    rendererRef.current?.applyFilter(filter);
  }, [filter]);

  // Re-apply themed styles when ``light`` toggles on <html>.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const html = document.documentElement;
    const obs = new MutationObserver(() => rendererRef.current?.applyTheme());
    obs.observe(html, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  // Push kind toggles to the renderer.
  useEffect(() => {
    if (!rendererRef.current) return;
    for (const k of ALL_NODE_KINDS) {
      rendererRef.current.setKindHidden(k, hiddenKinds.has(k));
    }
  }, [hiddenKinds, query.data]);

  useEffect(() => {
    if (!rendererRef.current) return;
    for (const k of ALL_EDGE_KINDS) {
      rendererRef.current.setEdgeKindHidden(k, hiddenEdgeKinds.has(k));
    }
  }, [hiddenEdgeKinds, query.data]);

  const stats = query.data?.stats;
  const kindCounts = useMemo(() => stats?.kinds ?? {}, [stats]);
  const visibleKinds = useMemo(
    () => ALL_NODE_KINDS.filter((k) => (kindCounts[k] ?? 0) > 0),
    [kindCounts],
  );

  // Per-layer node tallies. Prefer the v2 ``perLayer`` stat; fall back to
  // summing per-kind counts so legacy AST-only graphs still light up the
  // segmented control.
  const layerCounts = useMemo(() => {
    const out: Record<GraphLayer, number> = { ast: 0, entity: 0, memory: 0 };
    for (const layer of LAYER_ORDER) {
      const fromStat = stats?.perLayer?.[layer];
      if (typeof fromStat === "number") {
        out[layer] = fromStat;
        continue;
      }
      out[layer] = KnowledgeGraphRenderer.kindsForLayer(layer).reduce(
        (sum, k) => sum + (kindCounts[k] ?? 0),
        0,
      );
    }
    return out;
  }, [stats, kindCounts]);

  // Only surface the segmented control once the graph actually spans more
  // than one layer — a pure AST graph keeps the toolbar uncluttered.
  const presentLayers = useMemo(
    () => LAYER_ORDER.filter((l) => layerCounts[l] > 0),
    [layerCounts],
  );

  const toggleKind = (k: GraphNodeKind): void => {
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  const toggleEdgeKind = (k: GraphEdgeKind): void => {
    setHiddenEdgeKinds((prev) => {
      const next = new Set(prev);
      if (next.has(k)) next.delete(k);
      else next.add(k);
      return next;
    });
  };

  // A layer counts as "shown" if any of its node kinds is visible. Toggling
  // bulk-sets every kind in the layer — a thin wrapper over the same
  // ``hiddenKinds`` machinery the per-kind chips drive. Edges follow via the
  // renderer's endpoint-hidden cascade, so no edge kinds are enumerated here.
  const layerShown = (layer: GraphLayer): boolean =>
    KnowledgeGraphRenderer.kindsForLayer(layer).some((k) => !hiddenKinds.has(k));

  const toggleLayer = (layer: GraphLayer): void => {
    const kinds = KnowledgeGraphRenderer.kindsForLayer(layer);
    const hide = layerShown(layer); // currently visible → hide it
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      for (const k of kinds) {
        if (hide) next.add(k);
        else next.delete(k);
      }
      return next;
    });
  };

  const closePanel = (): void => {
    setSelected(null);
    rendererRef.current?.clearFocus();
    setFocused(false);
  };

  const clearFocus = (): void => {
    rendererRef.current?.clearFocus();
    setFocused(false);
  };

  const inByKind = useMemo(
    () => groupEdgesByKind(selected?.inEdges ?? []),
    [selected],
  );
  const outByKind = useMemo(
    () => groupEdgesByKind(selected?.outEdges ?? []),
    [selected],
  );

  return (
    <div className="flex flex-col flex-1 min-h-0">
      <WikiTopBar repo={slug} showBackToAll />

      <div className="flex-1 min-h-0 flex flex-col">
        {/* Toolbar */}
        <div className="border-b border-[hsl(var(--border))] px-4 sm:px-6 py-2 flex items-center gap-3 flex-wrap">
          <div className="relative w-full max-w-[280px]">
            <Search className="h-3.5 w-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
            <input
              type="search"
              placeholder="Filter nodes by name or file…"
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
                <span className="font-mono text-[hsl(var(--foreground))]">
                  {stats.nodeCount}
                </span>{" "}
                nodes
                <span className="mx-1.5 opacity-30">·</span>
                <span className="font-mono text-[hsl(var(--foreground))]">
                  {stats.edgeCount}
                </span>{" "}
                edges
              </span>
              <span className="opacity-30">|</span>
              {visibleKinds.map((k) => {
                const isHidden = hiddenKinds.has(k);
                return (
                  <button
                    key={k}
                    type="button"
                    onClick={() => toggleKind(k)}
                    aria-pressed={!isHidden}
                    title={isHidden ? `Show ${k} nodes` : `Hide ${k} nodes`}
                    className={cn(
                      "inline-flex items-center gap-1.5 px-2 h-6 rounded-full border text-[11px] transition-opacity",
                      "border-[hsl(var(--border))] bg-[hsl(var(--card))]",
                      "hover:bg-[hsl(var(--muted))]/40",
                      isHidden && "opacity-40 line-through",
                    )}
                  >
                    <span
                      className={cn("w-2 h-2 rounded-full", KIND_DOT[k])}
                    />
                    <span className="text-[hsl(var(--foreground))]">{k}</span>
                    <span className="font-mono text-[hsl(var(--muted-foreground))]">
                      {kindCounts[k]}
                    </span>
                  </button>
                );
              })}

              <Popover>
                <PopoverTrigger asChild>
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[11px] hover:bg-[hsl(var(--muted))]/40"
                  >
                    Edges
                    <ChevronDown className="h-3 w-3" />
                  </button>
                </PopoverTrigger>
                <PopoverContent align="start" className="w-56 p-2">
                  <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] px-1 pb-1">
                    Edge types
                  </div>
                  <div className="flex flex-col">
                    {ALL_EDGE_KINDS.map((k) => {
                      const isHidden = hiddenEdgeKinds.has(k);
                      return (
                        <button
                          key={k}
                          type="button"
                          onClick={() => toggleEdgeKind(k)}
                          aria-pressed={!isHidden}
                          className={cn(
                            "flex items-center gap-2 px-2 py-1.5 rounded text-[11px] text-left",
                            "hover:bg-[hsl(var(--muted))]/40",
                            isHidden && "opacity-40 line-through",
                          )}
                        >
                          <span
                            className={cn(
                              "w-3 h-[2px] rounded-full",
                              EDGE_DOT[k],
                            )}
                          />
                          <span className="text-[hsl(var(--foreground))] font-mono">
                            {k}
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
                              ? `Hide ${LAYER_LABEL[layer]} layer`
                              : `Show ${LAYER_LABEL[layer]} layer`
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
                              LAYER_DOT[layer],
                              !shown && "opacity-40",
                            )}
                          />
                          <span>{LAYER_LABEL[layer]}</span>
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

          {focused && (
            <button
              type="button"
              onClick={clearFocus}
              className="text-[11px] text-[hsl(var(--primary))] hover:underline px-2 h-7"
            >
              Clear focus
            </button>
          )}
          <button
            type="button"
            onClick={() => rendererRef.current?.relayout()}
            className="text-[11px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] px-2 h-7"
          >
            Re-layout
          </button>
        </div>

        {/* Canvas + side panel */}
        <div className="flex-1 min-h-0 flex">
          <div className="flex-1 min-w-0 relative">
            <div
              ref={canvasRef}
              className="absolute inset-0 bg-[hsl(var(--background))]"
            />

            {/* Floating zoom controls — bottom-right of the canvas. */}
            <div className="absolute bottom-3 right-3 z-10 flex flex-col rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-md overflow-hidden">
              <button
                type="button"
                onClick={() => rendererRef.current?.zoomBy(1.25)}
                aria-label="Zoom in"
                title="Zoom in"
                className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50"
              >
                <ZoomIn className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => rendererRef.current?.zoomBy(0.8)}
                aria-label="Zoom out"
                title="Zoom out"
                className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]"
              >
                <ZoomOut className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => rendererRef.current?.fit()}
                aria-label="Fit to view"
                title="Fit to view"
                className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]"
              >
                <Maximize2 className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={() => {
                  rendererRef.current?.reset();
                  setFocused(false);
                  setSelected(null);
                }}
                aria-label="Reset view"
                title="Reset view (clear focus, fit all)"
                className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]"
              >
                <RotateCcw className="h-4 w-4" />
              </button>
            </div>

            {query.isPending && (
              <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))]">
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                <span className="text-xs">Loading knowledge graph…</span>
              </div>
            )}
            {query.isError && (
              <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-xs">
                Graph unavailable. Re-index this project and try again.
              </div>
            )}
            {!query.isPending &&
              !query.isError &&
              (query.data?.nodes.length ?? 0) === 0 && (
                <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-xs">
                  No graph data persisted for this project yet.
                </div>
              )}
          </div>

          {selected && (
            <aside className="w-[340px] shrink-0 border-l border-[hsl(var(--border))] bg-[hsl(var(--card))] flex flex-col">
              <header className="px-3 py-2 border-b border-[hsl(var(--border))] flex items-center gap-2">
                <span
                  className={cn("w-2.5 h-2.5 rounded-full", KIND_DOT[selected.kind])}
                />
                <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
                  {selected.kind}
                </span>
                {selected.layer !== "ast" && (
                  <span className="text-[9px] uppercase tracking-wide px-1.5 py-px rounded-full bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))]">
                    {LAYER_LABEL[selected.layer]}
                  </span>
                )}
                <span className="text-xs font-mono truncate flex-1">
                  {selected.label}
                </span>
                <button
                  type="button"
                  onClick={closePanel}
                  aria-label="Close"
                  className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </header>
              <div className="px-3 py-3 space-y-3 overflow-y-auto text-xs">
                {selected.file ? (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
                      File
                    </div>
                    <code className="font-mono break-all">{selected.file}</code>
                    <div className="text-[hsl(var(--muted-foreground))] mt-1">
                      bytes {selected.range[0]}–{selected.range[1]} ·{" "}
                      <span className="font-mono">{selected.degree}</span>{" "}
                      connections
                    </div>
                  </div>
                ) : (
                  <div className="text-[hsl(var(--muted-foreground))]">
                    <span className="font-mono">{selected.degree}</span> connections
                  </div>
                )}
                {selected.entityType && (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
                      Type
                    </div>
                    <code className="font-mono">{selected.entityType}</code>
                  </div>
                )}
                {selected.labels && selected.labels.length > 0 && (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
                      Labels
                    </div>
                    <div className="flex flex-wrap gap-1">
                      {selected.labels.map((l) => (
                        <span
                          key={l}
                          className="px-1.5 py-px rounded-full text-[10px] bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))] font-mono"
                        >
                          {l}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
                {selected.snippet && (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
                      Memory
                    </div>
                    <p className="whitespace-pre-wrap text-[hsl(var(--muted-foreground))]">
                      {selected.snippet}
                    </p>
                  </div>
                )}
                {selected.docstring && (
                  <div>
                    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
                      Docstring
                    </div>
                    <p className="whitespace-pre-wrap text-[hsl(var(--muted-foreground))]">
                      {selected.docstring}
                    </p>
                  </div>
                )}
                {selected.outEdges.length > 0 && (
                  <EdgeSection
                    title={`Outgoing (${selected.outEdges.length})`}
                    grouped={outByKind}
                  />
                )}
                {selected.inEdges.length > 0 && (
                  <EdgeSection
                    title={`Incoming (${selected.inEdges.length})`}
                    grouped={inByKind}
                  />
                )}
              </div>
            </aside>
          )}
        </div>
      </div>
    </div>
  );
}

function groupEdgesByKind(
  edges: EdgeInfo[],
): Array<{ kind: GraphEdgeKind; items: EdgeInfo[] }> {
  const buckets = new Map<GraphEdgeKind, EdgeInfo[]>();
  for (const e of edges) {
    const list = buckets.get(e.kind) ?? [];
    list.push(e);
    buckets.set(e.kind, list);
  }
  return [...buckets.entries()].map(([kind, items]) => ({ kind, items }));
}

interface EdgeSectionProps {
  title: string;
  grouped: Array<{ kind: GraphEdgeKind; items: EdgeInfo[] }>;
}

function EdgeSection({ title, grouped }: EdgeSectionProps): JSX.Element {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
        {title}
      </div>
      <div className="flex flex-col gap-1.5">
        {grouped.map(({ kind, items }) => (
          <div key={kind}>
            <div className="flex items-center gap-1.5 mb-0.5">
              <span className={cn("w-3 h-[2px] rounded-full", EDGE_DOT[kind])} />
              <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))]">
                {kind} · {items.length}
              </span>
            </div>
            <ul className="ml-4 space-y-0.5">
              {items.slice(0, 12).map((e, i) => (
                <li
                  key={`${e.otherId}-${i}`}
                  className="flex items-center gap-1.5 truncate"
                >
                  <span
                    className={cn(
                      "w-1.5 h-1.5 rounded-full shrink-0",
                      KIND_DOT[e.otherKind],
                    )}
                  />
                  <span className="font-mono truncate text-[11px]">
                    {e.otherLabel || e.otherId}
                  </span>
                </li>
              ))}
              {items.length > 12 && (
                <li className="text-[10px] text-[hsl(var(--muted-foreground))] ml-3">
                  +{items.length - 12} more
                </li>
              )}
            </ul>
          </div>
        ))}
      </div>
    </div>
  );
}
