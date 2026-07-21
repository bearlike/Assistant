/**
 * Graph3DView's toolbar strip — filter input, node-count/edge-count summary,
 * the "Node types" visibility popover, and the per-layer toggle group.
 * Purely presentational: every derivation (kind counts, layer counts, which
 * kinds/layers are actually present) is computed by the caller and handed in
 * as props, so this component owns no graph logic of its own.
 */
import { Search, X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";
import type { Graph3DStats, Graph3DTheme } from "./Graph3DView";

export interface Graph3DToolbarProps {
  filter: string;
  onFilterChange: (value: string) => void;
  stats: Graph3DStats | null;
  theme: Graph3DTheme;
  /** Kinds present in the current stats (count > 0), in toolbar order. */
  visibleKinds: string[];
  kindCounts: Partial<Record<string, number>>;
  hiddenKinds: Set<string>;
  onToggleKind: (kind: string) => void;
  /** Layers present in the current stats (count > 0), in toolbar order. */
  presentLayers: string[];
  layerCounts: Record<string, number>;
  isLayerShown: (layer: string) => boolean;
  onToggleLayer: (layer: string) => void;
  /** Trailing hint text (e.g. "Click a folder to expand · drag to orbit"). */
  hint?: string;
}

export function Graph3DToolbar({
  filter,
  onFilterChange,
  stats,
  theme,
  visibleKinds,
  kindCounts,
  hiddenKinds,
  onToggleKind,
  presentLayers,
  layerCounts,
  isLayerShown,
  onToggleLayer,
  hint,
}: Graph3DToolbarProps) {
  return (
    <div className="border-b border-[hsl(var(--border))] px-4 sm:px-6 py-2 flex items-center gap-3 flex-wrap">
      <div className="relative w-full max-w-[280px]">
        <Search className="h-3.5 w-3.5 absolute left-2 top-1/2 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
        <Input
          type="search"
          placeholder="Filter nodes by name…"
          value={filter}
          onChange={(e) => onFilterChange(e.target.value)}
          className={cn(cardSurface({ radius: "right" }), "w-full pl-6 pr-6 h-8 shadow-none placeholder:text-[hsl(var(--muted-foreground))]")}
        />
        {filter && (
          <button
            type="button"
            onClick={() => onFilterChange("")}
            aria-label="Clear filter"
            className="absolute right-1 top-1/2 -translate-y-1/2 p-1 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]"
          >
            <X className="h-3 w-3" />
          </button>
        )}
      </div>

      {stats && (
        <div className="flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
          <span className="mr-1">
            <span className="text-[hsl(var(--foreground))]">{stats.nodeCount}</span>{" "}
            nodes
            <span className="mx-1.5 opacity-30">·</span>
            <span className="text-[hsl(var(--foreground))]">{stats.edgeCount}</span>{" "}
            edges
            {typeof stats.folderCount === "number" && (
              <>
                <span className="mx-1.5 opacity-30">·</span>
                <span className="text-[hsl(var(--foreground))]">
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
                className="inline-flex items-center gap-1 px-2 h-6 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-2xs hover:bg-[hsl(var(--muted))]/40"
              >
                Node types
              </button>
            </PopoverTrigger>
            <PopoverContent align="start" className="w-56 p-2">
              <div className="text-2xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] px-1 pb-1">
                Node types
              </div>
              <div className="flex flex-col">
                {visibleKinds.map((k) => {
                  const isHidden = hiddenKinds.has(k);
                  return (
                    <button
                      key={k}
                      type="button"
                      onClick={() => onToggleKind(k)}
                      aria-pressed={!isHidden}
                      className={cn(
                        "flex items-center gap-2 px-2 py-1.5 rounded text-2xs text-left",
                        "hover:bg-[hsl(var(--muted))]/40",
                        isHidden && "opacity-40 line-through",
                      )}
                    >
                      <span className={cn("w-2 h-2 rounded-full", theme.kindDot[k])} />
                      <span className="text-[hsl(var(--foreground))] flex-1">
                        {theme.kindLabel[k] ?? k}
                      </span>
                      <span className="text-[hsl(var(--muted-foreground))]">
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
                  const shown = isLayerShown(layer);
                  return (
                    <Button
                      key={layer}
                      variant="ghost"
                      size="sm"
                      onClick={() => onToggleLayer(layer)}
                      aria-pressed={shown}
                      title={
                        shown
                          ? `Hide ${theme.layerLabel[layer]} layer`
                          : `Show ${theme.layerLabel[layer]} layer`
                      }
                      className={cn(
                        "h-6 gap-1.5 px-2.5 rounded-none text-2xs",
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
                      <span className="text-[hsl(var(--muted-foreground))]">
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
        <span className="text-2xs text-[hsl(var(--muted-foreground))] hidden sm:inline">
          {hint}
        </span>
      )}
    </div>
  );
}

