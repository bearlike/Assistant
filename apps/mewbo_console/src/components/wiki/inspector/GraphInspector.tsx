/**
 * GraphInspector — the atomic dispatcher for the 3D Code Galaxy context
 * sidebar ("inspector").
 *
 * Selecting anything in the scene yields a {@link GraphSelection}; this
 * component holds a REGISTRY mapping every `GraphSelectionKind` to its typed
 * panel and renders the match. The registry is an exhaustive closed `Record`
 * over the union — a missing kind is a TS error, never a silent default that
 * hides it. When nothing is selected it renders a tidy placeholder.
 *
 * Panels render purely from the passed `graph` + `selection`; neighbour/edge
 * lookups go through a single {@link GraphIndex} built once per graph (memoised
 * on graph identity) so no panel re-scans O(E).
 */

import { useMemo } from "react";
import { MousePointerClick, X } from "lucide-react";
import { Button } from "@/components/ui/button";

import type {
  GraphSelection,
  GraphSelectionKind,
  KnowledgeGraph,
} from "../api/types";
import type { PlatformId } from "../router";
import { GraphIndex, type InspectorNode } from "./GraphIndex";

import { FolderPanel } from "./FolderPanel";
import { FilePanel } from "./FilePanel";
import { ModulePanel } from "./ModulePanel";
import { SymbolPanel } from "./SymbolPanel";
import { ExternalPanel } from "./ExternalPanel";
import { EntityPanel } from "./EntityPanel";
import { MemoryPanel } from "./MemoryPanel";
import { EdgePanel } from "./EdgePanel";

export interface GraphInspectorProps {
  selection: GraphSelection | null;
  /** The graph the selection indexes into; `null` while it's still loading. */
  graph: KnowledgeGraph | null;
  /** Focus/navigate to another node in the scene (drives the panels' links). */
  onNavigate?: (nodeId: string) => void;
  /** Dismiss the inspector (clears the scene selection). */
  onClose?: () => void;
  /** Canonical slug + platform — threaded to FilePanel's wiki-page link only. */
  slug?: string;
  platform?: PlatformId;
}

/** Everything a registry entry needs to render its panel. */
interface PanelContext {
  selection: GraphSelection;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
  slug?: string;
  platform?: PlatformId;
}

/**
 * The closed registry: every selection kind → its panel renderer. Node-backed
 * kinds resolve `selection.node` (guarded once below); `edge` reads
 * `selection.edge` + `aggregated`. Exhaustive by `Record<GraphSelectionKind,…>`.
 */
const REGISTRY: Record<GraphSelectionKind, (ctx: PanelContext) => JSX.Element | null> = {
  folder: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <FolderPanel node={node} index={index} onNavigate={onNavigate} />
    )),
  file: ({ selection, index, onNavigate, slug, platform }) =>
    withNode(selection, (node) => (
      <FilePanel
        node={node}
        index={index}
        slug={slug}
        platform={platform}
        onNavigate={onNavigate}
      />
    )),
  module: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <ModulePanel node={node} index={index} onNavigate={onNavigate} />
    )),
  symbol: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <SymbolPanel node={node} index={index} onNavigate={onNavigate} />
    )),
  external: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <ExternalPanel node={node} index={index} onNavigate={onNavigate} />
    )),
  entity: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <EntityPanel node={node} index={index} onNavigate={onNavigate} />
    )),
  memory: ({ selection, index, onNavigate }) =>
    withNode(selection, (node) => (
      <MemoryPanel node={node} index={index} onNavigate={onNavigate} />
    )),
  edge: ({ selection, index, onNavigate }) =>
    selection.edge ? (
      <EdgePanel
        edge={selection.edge}
        aggregated={selection.aggregated}
        index={index}
        onNavigate={onNavigate}
      />
    ) : (
      <MalformedSelection what="edge" />
    ),
};

export function GraphInspector({
  selection,
  graph,
  onNavigate,
  onClose,
  slug,
  platform,
}: GraphInspectorProps) {
  // One index per graph — rebuilt only when the graph object identity changes
  // (the query cache hands back a stable reference between renders). Null graph
  // (still loading) yields an empty index so the placeholder shows.
  const index = useMemo(() => (graph ? new GraphIndex(graph) : null), [graph]);

  const body =
    selection && index ? (
      REGISTRY[selection.kind]({ selection, index, onNavigate, slug, platform })
    ) : (
      <EmptyState />
    );

  return <InspectorFrame onClose={onClose}>{body}</InspectorFrame>;
}

/** The dismissible frame the inspector body sits in — header close button only
 *  renders when `onClose` is supplied; otherwise the body fills the panel. */
function InspectorFrame({
  onClose,
  children,
}: {
  onClose?: () => void;
  children: React.ReactNode;
}) {
  if (!onClose) return <>{children}</>;
  return (
    <div className="relative h-full">
      <Button
        variant="ghost"
        size="sm"
        onClick={onClose}
        aria-label="Close inspector"
        title="Close inspector"
        className="absolute right-2 top-2 z-10 h-7 w-7 p-0 text-[hsl(var(--muted-foreground))]"
      >
        <X className="h-3.5 w-3.5" />
      </Button>
      {children}
    </div>
  );
}

/** Resolve a node-backed selection, or surface a malformed-selection notice. */
function withNode(
  selection: GraphSelection,
  render: (node: InspectorNode) => JSX.Element,
): JSX.Element {
  if (selection.node) return render(selection.node);
  return <MalformedSelection what={selection.kind} />;
}

/** Tidy idle state shown when nothing is selected. */
function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center h-full gap-2 px-6 text-center text-[hsl(var(--muted-foreground))]">
      <MousePointerClick className="h-5 w-5 opacity-60" />
      <p className="text-xs">
        Select a node or relationship in the graph to inspect it.
      </p>
    </div>
  );
}

/** Defensive notice — a kind arrived without the payload it requires. */
function MalformedSelection({ what }: { what: string }) {
  return (
    <div className="flex items-center justify-center h-full px-6 text-center text-[11px] text-[hsl(var(--muted-foreground))]">
      Nothing to show for this {what} selection.
    </div>
  );
}
