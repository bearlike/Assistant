import { Database, Network, StickyNote, Workflow } from "lucide-react"

import { cardSurface } from "@/components/ui/card-surface"
import { cn } from "@/lib/utils"
import { useWorkspaceGraphSummary } from "../../hooks/useAgenticSearch"
import type { Workspace } from "../../types/agenticSearch"

/** One health stat — icon + value + label, with a quiet skeleton while the
 *  graph stats load. KISS: a flat row, no card chrome. */
function HealthStat({
  icon,
  value,
  label,
  loading,
  title,
}: {
  icon: React.ReactNode
  value: string
  label: string
  loading: boolean
  title?: string
}) {
  return (
    <span className="inline-flex items-center gap-1.5" title={title}>
      <span className="text-[hsl(var(--muted-foreground))]">{icon}</span>
      {loading ? (
        <span className="inline-block h-3 w-8 rounded bg-[hsl(var(--muted))] animate-pulse" />
      ) : (
        <span className="font-medium text-[hsl(var(--foreground))] tabular-nums">{value}</span>
      )}
      <span className="text-[hsl(var(--muted-foreground))]">{label}</span>
    </span>
  )
}

/**
 * Active-workspace health band. Reads the workspace SCG graph's stats —
 * mapped-source coverage, graph size (nodes·edges), and memory-note count —
 * via the lightweight `GET /workspaces/<id>/graph/summary` projection,
 * so the landing never downloads the full node/edge graph just to render four
 * numbers (the full graph stays lazy on the dialog). Degrades gracefully: an
 * unmapped / SCG-disabled workspace returns empty stats (every source in
 * `stats.unmapped`), so the band reads "0/N mapped" and links to the map flow
 * rather than erroring.
 */
export function WorkspaceHealthBand({
  workspace,
  onOpenGraph,
}: {
  workspace: Workspace
  onOpenGraph: (workspace: Workspace) => void
}) {
  const summaryQuery = useWorkspaceGraphSummary(workspace.id)
  const loading = summaryQuery.isPending
  const stats = summaryQuery.data?.stats
  const total = workspace.sources.length
  // `stats.unmapped` lists workspace sources with no SCG graph yet; mapped =
  // total − unmapped. Before the graph resolves, fall back to total so the
  // copy reads sensibly under the skeleton.
  const unmapped = stats?.unmapped.length ?? 0
  const mapped = Math.max(0, total - unmapped)
  const fullyMapped = !loading && unmapped === 0
  const memoryNotes = stats?.perLayer.memory ?? 0

  return (
    <button
      type="button"
      onClick={() => onOpenGraph(workspace)}
      title="Open the workspace capability graph"
      className={cn(
        cardSurface({ radius: "left" }),
        "mt-5 inline-flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 px-3.5 py-2 text-xs text-[hsl(var(--muted-foreground))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] transition-colors",
      )}
    >
      <HealthStat
        icon={<Database className="h-3.5 w-3.5" />}
        value={`${mapped}/${total}`}
        label={total === 1 ? "source mapped" : "sources mapped"}
        loading={loading}
        title={
          fullyMapped
            ? "Every source is mapped into the capability graph"
            : `${unmapped} source${unmapped === 1 ? "" : "s"} not yet mapped`
        }
      />
      <span aria-hidden className="h-3 w-px bg-[hsl(var(--border))]" />
      <HealthStat
        icon={<Network className="h-3.5 w-3.5" />}
        value={`${stats?.totalNodes ?? 0}·${stats?.totalEdges ?? 0}`}
        label="graph nodes·edges"
        loading={loading}
        title="Capability-graph size (nodes · edges)"
      />
      <span aria-hidden className="h-3 w-px bg-[hsl(var(--border))]" />
      <HealthStat
        icon={<StickyNote className="h-3.5 w-3.5" />}
        value={`${memoryNotes}`}
        label={memoryNotes === 1 ? "memory note" : "memory notes"}
        loading={loading}
        title="Connector reachability notes in the memory layer"
      />
      {!loading && !fullyMapped && (
        <span className="inline-flex items-center gap-1 text-[hsl(var(--primary-text))]">
          <Workflow className="h-3.5 w-3.5" />
          Map sources
        </span>
      )}
    </button>
  )
}
