/**
 * EdgePanel — a selected relationship (edge).
 *
 * Shows source → target with the edge type (and verb label for `RELATES`). When
 * the edge is a collapsed folder↔folder supernode (`aggregated` present), it
 * shows the constituent edge count and an expandable list of the real edges
 * behind it, each linking to its endpoints.
 */

import { ArrowRight, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

import type { GraphIndex } from "./GraphIndex";
import type { KnowledgeGraphEdge } from "../api/types";
import { PanelShell } from "./PanelShell";
import type { InspectorNode } from "./GraphIndex";
import { EDGE_DOT, kindDot } from "./palette";
import { NodeLinkRow, Section } from "./parts";

export function EdgePanel({
  edge,
  aggregated,
  index,
  onNavigate,
}: {
  edge: KnowledgeGraphEdge;
  aggregated?: KnowledgeGraphEdge[];
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { source, target, kind, label } = edge.data;
  const sourceNode = index.node(source);
  const targetNode = index.node(target);

  return (
    <PanelShell kindLabel="Relationship" nodeKind="" title={kind}>
      <Section title="Type">
        <div className="flex items-center gap-1.5">
          <span className={cn("w-3 h-[2px] rounded-full", EDGE_DOT[kind])} />
          <span className="font-mono text-[11px]">{kind}</span>
          {label && (
            <span className="text-[10px] text-[hsl(var(--muted-foreground))] italic">
              {label}
            </span>
          )}
        </div>
      </Section>

      <Section title="Endpoints">
        <div className="space-y-1.5">
          <Endpoint role="From" id={source} node={sourceNode} onNavigate={onNavigate} />
          <div className="flex justify-center text-[hsl(var(--muted-foreground))]">
            <ArrowRight className="h-3 w-3" />
          </div>
          <Endpoint role="To" id={target} node={targetNode} onNavigate={onNavigate} />
        </div>
      </Section>

      {aggregated && aggregated.length > 0 && (
        <Section title={`Collapsed edges (${aggregated.length})`}>
          <details className="group">
            <summary className="flex items-center gap-1.5 cursor-pointer select-none list-none text-[11px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]">
              <ChevronRight className="h-3 w-3 transition-transform group-open:rotate-90" />
              {aggregated.length} constituent relationships
            </summary>
            <ul className="mt-1.5 space-y-1.5 border-l border-[hsl(var(--border))] pl-2">
              {aggregated.map((e) => (
                <ConstituentEdge key={e.data.id} edge={e} index={index} onNavigate={onNavigate} />
              ))}
            </ul>
          </details>
        </Section>
      )}
    </PanelShell>
  );
}

function Endpoint({
  role,
  id,
  node,
  onNavigate,
}: {
  role: string;
  id: string;
  /** The resolved endpoint node, or undefined when it's outside the graph. */
  node: InspectorNode | undefined;
  onNavigate?: (nodeId: string) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))] w-8 shrink-0">
        {role}
      </span>
      <div className="flex-1 min-w-0">
        {node ? (
          <NodeLinkRow node={node} onNavigate={onNavigate} />
        ) : (
          <span className="font-mono truncate text-[11px] text-[hsl(var(--muted-foreground))]">
            {id}
          </span>
        )}
      </div>
    </div>
  );
}

/** One constituent edge inside a collapsed supernode: from → kind → to. */
function ConstituentEdge({
  edge,
  index,
  onNavigate,
}: {
  edge: KnowledgeGraphEdge;
  index: GraphIndex;
  onNavigate?: (nodeId: string) => void;
}) {
  const { source, target, kind } = edge.data;
  const src = index.node(source);
  const tgt = index.node(target);
  return (
    <li className="text-[11px]">
      <div className="flex items-center gap-1 text-[10px] text-[hsl(var(--muted-foreground))] mb-0.5">
        <span className={cn("w-2.5 h-[2px] rounded-full", EDGE_DOT[kind])} />
        <span className="font-mono">{kind}</span>
      </div>
      <div className="ml-3 flex items-center gap-1 truncate">
        <span className={cn("w-1.5 h-1.5 rounded-full shrink-0", kindDot(src?.data.kind ?? "File"))} />
        <button
          type="button"
          disabled={!onNavigate}
          onClick={() => onNavigate?.(source)}
          className="font-mono truncate hover:text-[hsl(var(--primary))] disabled:hover:text-inherit"
        >
          {src?.data.label ?? source}
        </button>
        <ArrowRight className="h-2.5 w-2.5 shrink-0 text-[hsl(var(--muted-foreground))]" />
        <span className={cn("w-1.5 h-1.5 rounded-full shrink-0", kindDot(tgt?.data.kind ?? "File"))} />
        <button
          type="button"
          disabled={!onNavigate}
          onClick={() => onNavigate?.(target)}
          className="font-mono truncate hover:text-[hsl(var(--primary))] disabled:hover:text-inherit"
        >
          {tgt?.data.label ?? target}
        </button>
      </div>
    </li>
  );
}
