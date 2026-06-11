/**
 * Workspace SCG graph viewer (#79) — the search-side twin of the wiki
 * ``KnowledgeGraphScreen``, rendered inside a shadcn ``Dialog`` so it overlays
 * the search surface from a workspace card / results rail entry point.
 *
 * It REUSES the wiki ``KnowledgeGraphRenderer`` engine wholesale (one Cytoscape
 * canvas, focus mode, filter, theming, layout) by injecting the search-domain
 * ``SCG_RENDER_CONFIG`` — no fork of the renderer. This component owns only the
 * React lifecycle, the per-layer/kind toggles (closed-union Record maps), the
 * node inspector (capability schema / recipe / anchored memory notes), and the
 * unmapped-ghost "map this source" hint.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Loader2, Maximize2, RotateCcw, Search, X, ZoomIn, ZoomOut } from "lucide-react";

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
  KnowledgeGraphRenderer,
  type EdgeInfo,
  type NodeClickHandler,
} from "../../wiki/KnowledgeGraphRenderer";
import { useWorkspaceGraph } from "../../../hooks/useAgenticSearch";
import type { Workspace } from "../../../types/agenticSearch";
import {
  SCG_ALL_NODE_KINDS,
  SCG_KIND_DOT,
  SCG_KIND_LABEL,
  SCG_LAYER_DOT,
  SCG_LAYER_LABEL,
  SCG_LAYER_ORDER,
  SCG_RENDER_CONFIG,
  scgKindsForLayer,
} from "./scgGraphConfig";
import type {
  ScgEdgeKind,
  ScgGraphLayer,
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

/** Split out so the renderer only mounts while the dialog is open. */
function GraphBody({
  workspace,
  onMapSource,
}: {
  workspace: Workspace;
  onMapSource?: () => void;
}) {
  const canvasRef = useRef<HTMLDivElement | null>(null);
  const rendererRef = useRef<KnowledgeGraphRenderer | null>(null);
  const [selected, setSelected] = useState<SelectedScgNode | null>(null);
  const [filter, setFilter] = useState("");
  const [hiddenKinds, setHiddenKinds] = useState<Set<ScgNodeKind>>(new Set());
  const [focused, setFocused] = useState(false);

  const query = useWorkspaceGraph(workspace.id);
  const graph = query.data;

  // Boot the renderer with the SCG config once the canvas div is attached.
  useEffect(() => {
    const el = canvasRef.current;
    if (!el || rendererRef.current) return;
    rendererRef.current = new KnowledgeGraphRenderer(el, SCG_RENDER_CONFIG);
    return () => {
      rendererRef.current?.dispose();
      rendererRef.current = null;
    };
  }, []);

  // Render data when it arrives. The renderer's wire shape is structurally the
  // wiki ``KnowledgeGraph``; the SCG payload matches it (nodes/edges {data}).
  useEffect(() => {
    if (!graph || !rendererRef.current) return;
    rendererRef.current.render(graph as unknown as Parameters<KnowledgeGraphRenderer["render"]>[0]);
    rendererRef.current.onNodeClick(((node) => {
      setSelected(node as unknown as SelectedScgNode);
      setFocused(true);
    }) as NodeClickHandler);
  }, [graph]);

  useEffect(() => {
    rendererRef.current?.applyFilter(filter);
  }, [filter]);

  // Re-theme on light/dark toggle (same MutationObserver idiom as the wiki).
  useEffect(() => {
    if (typeof window === "undefined") return;
    const obs = new MutationObserver(() => rendererRef.current?.applyTheme());
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => obs.disconnect();
  }, []);

  // Push kind toggles to the renderer.
  useEffect(() => {
    if (!rendererRef.current) return;
    for (const k of SCG_ALL_NODE_KINDS) {
      rendererRef.current.setKindHidden(
        k as never,
        hiddenKinds.has(k),
      );
    }
  }, [hiddenKinds, graph]);

  const stats = graph?.stats;
  const kindCounts = useMemo(() => stats?.kinds ?? {}, [stats]);
  const visibleKinds = useMemo(
    () => SCG_ALL_NODE_KINDS.filter((k) => (kindCounts[k] ?? 0) > 0),
    [kindCounts],
  );

  // Per-layer node tallies (prefer the perLayer stat; ghost nodes ride schema).
  const layerCounts = useMemo(() => {
    const out: Record<ScgGraphLayer, number> = { schema: 0, memory: 0, entity: 0 };
    for (const layer of SCG_LAYER_ORDER) {
      const fromStat = stats?.perLayer?.[layer];
      out[layer] =
        typeof fromStat === "number"
          ? fromStat
          : scgKindsForLayer(layer).reduce((s, k) => s + (kindCounts[k] ?? 0), 0);
    }
    // ghost (unmapped) nodes count toward the schema layer toggle.
    out.schema += kindCounts.unmapped ?? 0;
    return out;
  }, [stats, kindCounts]);

  const presentLayers = useMemo(
    () => SCG_LAYER_ORDER.filter((l) => layerCounts[l] > 0),
    [layerCounts],
  );

  const toggleKind = (k: ScgNodeKind): void =>
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      next.has(k) ? next.delete(k) : next.add(k);
      return next;
    });

  const layerShown = (layer: ScgGraphLayer): boolean =>
    scgKindsForLayer(layer).some((k) => !hiddenKinds.has(k));

  const toggleLayer = (layer: ScgGraphLayer): void => {
    const kinds = scgKindsForLayer(layer);
    const hide = layerShown(layer);
    setHiddenKinds((prev) => {
      const next = new Set(prev);
      for (const k of kinds) (hide ? next.add(k) : next.delete(k));
      return next;
    });
  };

  const closePanel = (): void => {
    setSelected(null);
    rendererRef.current?.clearFocus();
    setFocused(false);
  };

  const isEmpty = !query.isPending && !query.isError && (graph?.nodes.length ?? 0) === 0;
  const allUnmapped =
    !!graph && graph.nodes.length > 0 && graph.nodes.every((n) => n.data.unmapped);

  return (
    <div className="flex-1 min-h-0 flex flex-col">
      {/* Toolbar */}
      <div className="border-b border-[hsl(var(--border))] px-4 py-2 flex items-center gap-3 flex-wrap">
        <div className="relative w-full max-w-[260px]">
          <Search className="h-3.5 w-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
          <input
            type="search"
            placeholder="Filter nodes…"
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
          <div className="flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))] flex-wrap">
            <span className="mr-1">
              <span className="font-mono text-[hsl(var(--foreground))]">
                {stats.totalNodes}
              </span>{" "}
              nodes
              <span className="mx-1.5 opacity-30">·</span>
              <span className="font-mono text-[hsl(var(--foreground))]">
                {stats.totalEdges}
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
                  title={isHidden ? `Show ${SCG_KIND_LABEL[k]}` : `Hide ${SCG_KIND_LABEL[k]}`}
                  className={cn(
                    "inline-flex items-center gap-1.5 px-2 h-6 rounded-full border text-[11px] transition-opacity",
                    "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:bg-[hsl(var(--muted))]/40",
                    isHidden && "opacity-40 line-through",
                  )}
                >
                  <span className={cn("w-2 h-2 rounded-full", SCG_KIND_DOT[k])} />
                  <span className="text-[hsl(var(--foreground))]">{SCG_KIND_LABEL[k]}</span>
                  <span className="font-mono text-[hsl(var(--muted-foreground))]">
                    {kindCounts[k]}
                  </span>
                </button>
              );
            })}

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
                        title={shown ? `Hide ${SCG_LAYER_LABEL[layer]}` : `Show ${SCG_LAYER_LABEL[layer]}`}
                        className={cn(
                          "h-6 gap-1.5 px-2.5 rounded-none text-[11px]",
                          i > 0 && "border-l border-[hsl(var(--border))]",
                          shown
                            ? "bg-[hsl(var(--muted))]/40 text-[hsl(var(--foreground))]"
                            : "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--muted))]/20",
                        )}
                      >
                        <span
                          className={cn("w-2 h-2 rounded-full", SCG_LAYER_DOT[layer], !shown && "opacity-40")}
                        />
                        <span>{SCG_LAYER_LABEL[layer]}</span>
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
            onClick={() => {
              rendererRef.current?.clearFocus();
              setFocused(false);
            }}
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

      {/* Canvas + inspector */}
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0 relative">
          <div ref={canvasRef} className="absolute inset-0 bg-[hsl(var(--background))]" />

          <div className="absolute bottom-3 right-3 z-10 flex flex-col rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-md overflow-hidden">
            <button type="button" onClick={() => rendererRef.current?.zoomBy(1.25)} aria-label="Zoom in" title="Zoom in" className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50">
              <ZoomIn className="h-4 w-4" />
            </button>
            <button type="button" onClick={() => rendererRef.current?.zoomBy(0.8)} aria-label="Zoom out" title="Zoom out" className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]">
              <ZoomOut className="h-4 w-4" />
            </button>
            <button type="button" onClick={() => rendererRef.current?.fit()} aria-label="Fit to view" title="Fit to view" className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]">
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
              title="Reset view"
              className="h-8 w-8 flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/50 border-t border-[hsl(var(--border))]"
            >
              <RotateCcw className="h-4 w-4" />
            </button>
          </div>

          {query.isPending && (
            <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))]">
              <Loader2 className="h-4 w-4 animate-spin mr-2" />
              <span className="text-xs">Loading capability graph…</span>
            </div>
          )}
          {query.isError && (
            <div className="absolute inset-0 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-xs">
              Couldn't load the workspace graph.
            </div>
          )}
          {isEmpty && (
            <div className="absolute inset-0 flex flex-col items-center justify-center text-center gap-2 px-6">
              <span className="text-xs text-[hsl(var(--muted-foreground))]">
                This workspace has no sources to map yet.
              </span>
            </div>
          )}
          {allUnmapped && (
            <div className="absolute inset-x-0 top-3 flex justify-center px-6">
              <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))]/95 px-3 py-2 text-center text-[11px] text-[hsl(var(--muted-foreground))] shadow-[var(--elev-1)] [text-wrap:balance]">
                None of this workspace's sources are mapped yet — map a source to
                build its capability subgraph.
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
            </div>
          )}
        </div>

        {selected && (
          <NodeInspector node={selected} onClose={closePanel} onMapSource={onMapSource} />
        )}
      </div>
    </div>
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
              {(e.kind as ScgEdgeKind)}
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
