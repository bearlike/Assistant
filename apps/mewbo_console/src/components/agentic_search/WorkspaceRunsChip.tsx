import { useState } from "react"
import { History, Loader2 } from "lucide-react"

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { FOCUS_RING } from "@/components/ui/focus-ring"
import { cn } from "@/lib/utils"
import { useWorkspaceRuns } from "../../hooks/useAgenticSearch"
import { RelativeTime } from "../../utils/relativeTime"
import type { RunStatus, Workspace } from "../../types/agenticSearch"

/** Rows shown in a workspace card's "recent runs" popover. */
const MAX_RECENT_RUNS_SHOWN = 5

const RUN_STATUS_GLYPH: Record<RunStatus, string> = {
  queued: "·",
  running: "…",
  completed: "✓",
  failed: "✕",
  cancelled: "⊘",
}

/**
 * Compact run-history affordance on a workspace card. Lazy: the
 * `GET /workspaces/<id>/runs` query only runs once the popover opens.
 * Picking an entry rehydrates that run via the existing run-id state.
 */
export function WorkspaceRunsChip({
  workspace,
  onOpenRun,
}: {
  workspace: Workspace
  onOpenRun: (runId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const runsQuery = useWorkspaceRuns(open ? workspace.id : null)
  const runs = (runsQuery.data ?? []).slice(0, MAX_RECENT_RUNS_SHOWN)

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={`Recent runs in ${workspace.name}`}
          title="Recent runs"
          // Don't let the click bubble to the card (which picks the workspace).
          onClick={(e) => e.stopPropagation()}
          onKeyDown={(e) => e.stopPropagation()}
          // `flex-none whitespace-nowrap` is the single-line guarantee: the
          // "N past" pill never wraps to a second line, even at the 240px grid
          // floor. The History glyph is `flex-none` so only the count is text.
          className={`inline-flex flex-none items-center gap-1 px-1.5 h-6 rounded whitespace-nowrap text-2xs text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] ${FOCUS_RING} transition-colors`}
        >
          <History className="h-3 w-3 flex-none" />
          {workspace.past_queries?.length ?? 0} past
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-72 p-1 [box-shadow:var(--elev-3)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-2 py-1.5 text-2xs uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
          Recent runs
        </div>
        {runsQuery.isPending ? (
          <div className="flex items-center gap-2 px-2 py-2 text-xs text-[hsl(var(--muted-foreground))]">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading runs…
          </div>
        ) : runsQuery.isError ? (
          <div className="px-2 py-2 text-xs text-[hsl(var(--destructive-text))]">
            Couldn't load run history.
          </div>
        ) : runs.length === 0 ? (
          <div className="px-2 py-2 text-xs text-[hsl(var(--muted-foreground))]">
            No runs yet in this workspace.
          </div>
        ) : (
          <ul className="space-y-0.5">
            {runs.map((r) => (
              <li key={r.run_id}>
                <button
                  type="button"
                  onClick={() => {
                    setOpen(false)
                    onOpenRun(r.run_id)
                  }}
                  className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm hover:bg-[hsl(var(--accent))] transition-colors"
                >
                  <span
                    className={cn(
                      "text-2xs flex-none w-3 text-center",
                      r.status === "completed" && "text-[hsl(var(--success))]",
                      r.status === "failed" && "text-[hsl(var(--destructive-text))]",
                      r.status === "running" && "text-[hsl(var(--primary-text))]"
                    )}
                    title={r.status}
                  >
                    {RUN_STATUS_GLYPH[r.status] ?? "·"}
                  </span>
                  <span className="flex-1 truncate">{r.query}</span>
                  <span
                    className="text-2xs text-[hsl(var(--muted-foreground))] flex-none"
                    title={RelativeTime.tooltip(r.created_at)}
                  >
                    {RelativeTime.format(r.created_at)}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </PopoverContent>
    </Popover>
  )
}
