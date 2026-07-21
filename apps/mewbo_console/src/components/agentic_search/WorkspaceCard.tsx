import { Pencil, Workflow } from "lucide-react"

import { FOCUS_RING } from "@/components/ui/focus-ring"
import { cn } from "@/lib/utils"
import type { SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"
import { WorkspaceRunsChip } from "./WorkspaceRunsChip"

interface WorkspaceCardProps {
  workspace: Workspace
  /** Is this the currently active workspace (drives the selected chrome). */
  active: boolean
  sources: SourceCatalogEntry[]
  onSelect: (workspace: Workspace) => void
  onOpenConfig: (workspace: Workspace) => void
  onOpenGraph: (workspace: Workspace) => void
  onOpenRun: (runId: string) => void
}

/**
 * Workspace grid card. NOT migrated to `cardSurface()` — the conditional
 * selected/hover treatment (primary-tinted border/bg on the active card,
 * elev-1→elev-2 hover transition) is outside cardSurface's chrome-only
 * contract (selection-state ternary cards are a documented legitimate gap).
 */
export function WorkspaceCard({
  workspace: w,
  active,
  sources,
  onSelect,
  onOpenConfig,
  onOpenGraph,
  onOpenRun,
}: WorkspaceCardProps) {
  return (
    // div+role rather than <button> so the recent-runs popover trigger
    // (a real button) can nest inside without invalid interactive nesting.
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open workspace ${w.name}`}
      onClick={() => onSelect(w)}
      onKeyDown={(e) => {
        if (e.target !== e.currentTarget) return
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault()
          onSelect(w)
        }
      }}
      className={cn(
        "group flex flex-col gap-2.5 p-3.5 rounded-xl border text-left min-h-[120px] cursor-pointer",
        "[box-shadow:var(--elev-1)] hover:[box-shadow:var(--elev-2)] hover:-translate-y-px",
        "transition-[box-shadow,transform,background-color,border-color] duration-200",
        FOCUS_RING,
        active
          ? "border-[hsl(var(--primary)/0.5)] bg-[hsl(var(--primary)/0.04)]"
          : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)]"
      )}
    >
      {/* Name > description hierarchy; the body claims the slack
          (`flex-1`) so the meta/action row pins to the card bottom
          (`mt-auto`) regardless of description length — equal-height
          grid rows then read as a consistent shelf. */}
      <div className="flex flex-col gap-1 flex-1 min-w-0">
        <h4 className="text-sm font-medium leading-tight truncate">{w.name}</h4>
        <p className="text-xs text-[hsl(var(--muted-foreground))] [text-wrap:pretty] line-clamp-2">
          {w.desc}
        </p>
      </div>
      {/* Meta/action shelf — single line ALWAYS (`flex-nowrap`). The
          avatar rail shrinks/clips (`min-w-0 overflow-hidden`), the
          action cluster never does (`flex-none`), so the "N past" pill
          can't wrap to a second line at narrow grid widths. */}
      <div className="mt-auto flex items-center justify-between gap-2 flex-nowrap">
        <div className="flex items-center gap-1 min-w-0 overflow-hidden">
          {w.sources.slice(0, 5).map((sid) => (
            <SrcAvatar
              key={sid}
              source={sources.find((s) => s.id === sid)}
              size={20}
            />
          ))}
          {w.sources.length > 5 && (
            <span className="flex-none whitespace-nowrap text-2xs text-[hsl(var(--muted-foreground))] ml-1">
              +{w.sources.length - 5}
            </span>
          )}
        </div>
        <div className="flex items-center gap-0.5 flex-none">
          {/* Pure actions idle hidden and reveal on hover / focus-within
              (console hover-reveal pattern) so the resting card stays
              calm; the runs chip beside them is an info badge and stays
              put. Each control keeps a ≥24px (h-6 w-6) hit target. */}
          <div className="flex items-center gap-0.5 opacity-0 transition-opacity duration-150 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100">
            <button
              type="button"
              aria-label={`Configure workspace ${w.name}`}
              title="Edit purpose, instructions & sources"
              onClick={(e) => {
                e.stopPropagation()
                onOpenConfig(w)
              }}
              onKeyDown={(e) => e.stopPropagation()}
              className={`inline-flex items-center justify-center h-6 w-6 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] ${FOCUS_RING} transition-colors`}
            >
              <Pencil className="h-3 w-3" />
            </button>
            <button
              type="button"
              aria-label={`Capability graph for ${w.name}`}
              title="Capability graph"
              onClick={(e) => {
                e.stopPropagation()
                onOpenGraph(w)
              }}
              onKeyDown={(e) => e.stopPropagation()}
              className={`inline-flex items-center justify-center h-6 w-6 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] ${FOCUS_RING} transition-colors`}
            >
              <Workflow className="h-3 w-3" />
            </button>
          </div>
          <WorkspaceRunsChip workspace={w} onOpenRun={onOpenRun} />
        </div>
      </div>
    </div>
  )
}
