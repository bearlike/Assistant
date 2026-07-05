/**
 * Shared presentational atoms for the inspector panels.
 *
 * Every panel renders the same vocabulary — a small uppercase section label, a
 * kind-coloured dot, a row linking to another node, a grouped edge list, and a
 * markdown docstring. These live here once so the panels compose them instead
 * of each re-inlining the styling ported from the original 2D graph side
 * panel. KISS/DRY: one place decides how each atom looks.
 */

import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";

import { cn } from "@/lib/utils";

import { buildMarkdownComponents } from "../markdownComponents";
import type { AdjacentEdge, InspectorNode } from "./GraphIndex";
import { EDGE_DOT, groupEdgesByKind, kindDot } from "./palette";

// ── Section scaffolding ──────────────────────────────────────────────────

/** A small uppercase muted section label. */
export function SectionLabel({ children }: { children: React.ReactNode }) {
  return (
    <div className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1">
      {children}
    </div>
  );
}

/** A labelled block — `<SectionLabel>` + body. Renders nothing if `hidden`. */
export function Section({
  title,
  children,
  hidden,
}: {
  title: React.ReactNode;
  children: React.ReactNode;
  hidden?: boolean;
}) {
  if (hidden) return null;
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
      {children}
    </div>
  );
}

// ── Labels / pills ───────────────────────────────────────────────────────

/** A row of free-form classifier-label pills. */
export function LabelPills({ labels }: { labels: string[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {labels.map((l) => (
        <span
          key={l}
          className="px-1.5 py-px rounded-full text-[10px] bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))] font-mono"
        >
          {l}
        </span>
      ))}
    </div>
  );
}

// ── Node link rows ───────────────────────────────────────────────────────

/**
 * A clickable row that navigates to another node. The kind dot identifies the
 * target's kind; the monospace label truncates. `onNavigate` is optional — when
 * absent the row renders as static (no button affordance).
 */
export function NodeLinkRow({
  node,
  onNavigate,
}: {
  node: InspectorNode;
  onNavigate?: (nodeId: string) => void;
}) {
  const { id, label, kind } = node.data;
  const inner = (
    <>
      <span className={cn("w-1.5 h-1.5 rounded-full shrink-0", kindDot(kind))} />
      <span className="font-mono truncate text-[11px]">{label || id}</span>
    </>
  );
  if (!onNavigate) {
    return <div className="flex items-center gap-1.5 truncate py-0.5">{inner}</div>;
  }
  return (
    <button
      type="button"
      onClick={() => onNavigate(id)}
      className="flex w-full items-center gap-1.5 truncate py-0.5 text-left hover:text-[hsl(var(--primary))]"
    >
      {inner}
    </button>
  );
}

/**
 * A capped, scrollable list of node-link rows with a "+N more" tail — the
 * shared shape for "contained symbols", "calls", "related entities", etc.
 */
export function NodeLinkList({
  nodes,
  onNavigate,
  limit = 12,
  emptyHint,
}: {
  nodes: InspectorNode[];
  onNavigate?: (nodeId: string) => void;
  limit?: number;
  emptyHint?: string;
}) {
  if (nodes.length === 0) {
    return emptyHint ? (
      <div className="text-[11px] text-[hsl(var(--muted-foreground))] italic">
        {emptyHint}
      </div>
    ) : null;
  }
  return (
    <ul className="flex flex-col">
      {nodes.slice(0, limit).map((n) => (
        <li key={n.data.id}>
          <NodeLinkRow node={n} onNavigate={onNavigate} />
        </li>
      ))}
      {nodes.length > limit && (
        <li className="text-[10px] text-[hsl(var(--muted-foreground))] mt-0.5">
          +{nodes.length - limit} more
        </li>
      )}
    </ul>
  );
}

// ── Edge sections (grouped by kind, ported from the original 2D side panel) ─

/**
 * A grouped edge list — the `AdjacentEdge[]` for one direction, bucketed by
 * edge kind, each row linking to the node at the other end. Ports the
 * `EdgeSection` from the original side panel, but reads resolved neighbours
 * from the shared `GraphIndex` adjacency instead of re-walking the graph.
 */
export function EdgeSection({
  title,
  edges,
  onNavigate,
}: {
  title: string;
  edges: AdjacentEdge[];
  onNavigate?: (nodeId: string) => void;
}) {
  const grouped = useMemo(() => groupEdgesByKind(edges), [edges]);
  if (edges.length === 0) return null;
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
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
                <li key={`${e.otherId}-${i}`}>
                  <EdgeTargetRow edge={e} onNavigate={onNavigate} />
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

/** One edge target — the resolved neighbour, or a bare id when dangling. */
function EdgeTargetRow({
  edge,
  onNavigate,
}: {
  edge: AdjacentEdge;
  onNavigate?: (nodeId: string) => void;
}) {
  if (edge.other) return <NodeLinkRow node={edge.other} onNavigate={onNavigate} />;
  return (
    <div className="flex items-center gap-1.5 truncate py-0.5">
      <span className="w-1.5 h-1.5 rounded-full shrink-0 bg-[hsl(var(--graph-edge-soft))]" />
      <span className="font-mono truncate text-[11px] text-[hsl(var(--muted-foreground))]">
        {edge.otherId}
      </span>
    </div>
  );
}

// ── Docstring (markdown) ─────────────────────────────────────────────────

/** A docstring has no internal-page navigation target — relative links fall
 *  through to the renderer's external/relative handling. */
function noPageNav(): void {
  /* intentional no-op */
}

/**
 * Render a docstring / snippet as markdown via the shared wiki renderer
 * (`buildMarkdownComponents`, mermaid off — same config `LiveBlocks` uses).
 * Docstrings are short prose that may carry code spans / lists, so the one
 * canonical renderer keeps them consistent with page + Q&A prose.
 */
export function DocstringMarkdown({ text }: { text: string }) {
  const components = useMemo(
    () => buildMarkdownComponents({ onNavigatePage: noPageNav, enableMermaid: false }),
    [],
  );
  return (
    <div className="text-[hsl(var(--muted-foreground))] [&_p]:my-1.5 [&_p]:text-[12px] [&_p]:leading-[1.6]">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
