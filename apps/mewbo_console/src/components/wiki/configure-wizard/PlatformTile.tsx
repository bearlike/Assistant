/**
 * Wizard step 1 platform tile. Brand-colored 2-letter monogram + name +
 * one-line description + clay check on selection.
 *
 * Per design hard-rule: no copyrighted third-party logos. Monograms only.
 */

import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

import type { Platform } from "../api/types";
import { PlatformIcon } from "./PlatformIcon";

interface PlatformTileProps {
  platform: Platform;
  selected: boolean;
  onSelect: () => void;
}

export function PlatformTile({ platform, selected, onSelect }: PlatformTileProps) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      className={cn(
        "flex items-start gap-3 p-3 rounded-lg border text-left transition-all",
        selected
          ? "border-[hsl(var(--primary))] bg-[hsl(var(--primary))]/[0.06] ring-1 ring-[hsl(var(--primary))]/30"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--border-strong))] hover:bg-[hsl(var(--accent))]/40"
      )}
    >
      <span
        aria-hidden
        className="inline-flex items-center justify-center w-9 h-9 rounded-md shrink-0"
        style={{ background: platform.color }}
      >
        <PlatformIcon platformId={platform.id} className="h-[18px] w-[18px] text-white" />
      </span>
      <span className="flex-1 min-w-0">
        <span className="block text-sm font-medium text-[hsl(var(--foreground))]">
          {platform.name}
        </span>
        <span className="block text-[11px] text-[hsl(var(--muted-foreground))] [text-wrap:pretty]">
          {platform.short}
        </span>
      </span>
      {selected && (
        <span className="text-[hsl(var(--primary))] shrink-0">
          <Check className="h-4 w-4" />
        </span>
      )}
    </button>
  );
}
