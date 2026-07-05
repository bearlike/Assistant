/**
 * PanelShell — the common chrome every inspector panel wears.
 *
 * A header row (kind dot + uppercase kind word + optional non-AST layer badge +
 * monospace title) over a scrollable body. Ports the header treatment from the
 * original 2D graph side panel so all kinds read identically; each typed panel
 * supplies only its own body sections.
 */

import { cn } from "@/lib/utils";

import type { GraphLayer } from "../api/types";
import { LAYER_LABEL, kindDot } from "./palette";

export function PanelShell({
  kindLabel,
  nodeKind,
  layer,
  title,
  children,
}: {
  /** Uppercase word shown beside the dot (e.g. "FILE", "CLASS", "EDGE"). */
  kindLabel: string;
  /** Node kind that picks the dot colour (falls back to the soft edge tint). */
  nodeKind: string;
  /** Multiplex layer — a badge is shown for non-`ast` layers only. */
  layer?: GraphLayer;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col h-full">
      {/* `pr-8` reserves room for the inspector's floating close button so it
          never overlaps the truncating title. */}
      <header className="pl-3 pr-8 py-2 border-b border-[hsl(var(--border))] flex items-center gap-2 shrink-0">
        <span className={cn("w-2.5 h-2.5 rounded-full", kindDot(nodeKind))} />
        <span className="text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
          {kindLabel}
        </span>
        {layer && layer !== "ast" && (
          <span className="text-[9px] uppercase tracking-wide px-1.5 py-px rounded-full bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))]">
            {LAYER_LABEL[layer]}
          </span>
        )}
        <span className="text-xs font-mono truncate flex-1">{title}</span>
      </header>
      <div className="px-3 py-3 space-y-3 overflow-y-auto text-xs flex-1">
        {children}
      </div>
    </div>
  );
}
