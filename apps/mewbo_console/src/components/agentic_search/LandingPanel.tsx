import { useCallback, useMemo, useRef, useState } from "react"
import {
  AlertTriangle,
  ChevronDown,
  Database,
  History,
  Loader2,
  Network,
  Pencil,
  Plus,
  Search,
  StickyNote,
  Workflow,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import { useWorkspaceGraphSummary, useWorkspaceRuns } from "../../hooks/useAgenticSearch"
import { RelativeTime } from "../wiki/relativeTime"
import type {
  RunStatus,
  SearchTier,
  SourceCatalogEntry,
  Workspace,
} from "../../types/agenticSearch"
import { SearchBar } from "./SearchBar"
import { SrcAvatar } from "./SrcAvatar"
import { dedupePastQueries, pastQueryKey } from "./utils"

interface LandingPanelProps {
  workspace: Workspace
  workspaces: Workspace[]
  sources: SourceCatalogEntry[]
  tier: SearchTier
  onTierChange: (tier: SearchTier) => void
  /** Per-run model override ("" = tier default) — session-instance-only. */
  model: string
  onModelChange: (model: string) => void
  /** A run submission is in flight (mutation pending). */
  submitting?: boolean
  onPickWorkspace: (workspace: Workspace) => void
  onSubmit: (query: string) => void
  onOpenCreate: () => void
  onOpenConfig: (workspace: Workspace) => void
  onOpenSources: () => void
  /** Open a past run by id (rehydrates via the run snapshot / stream). */
  onOpenRun: (runId: string) => void
  /** Open a workspace's capability graph (#79). */
  onOpenGraph: (workspace: Workspace) => void
}

type Tab = "workspaces" | "recent"

/** Case-insensitive match over a workspace's name, description, and past-query text. */
function matchesWorkspace(w: Workspace, needle: string): boolean {
  const haystack = [w.name, w.desc, ...(w.past_queries ?? []).map((p) => p.q)]
  return haystack.some((s) => s.toLowerCase().includes(needle))
}

/**
 * Landing surface — hero rhythm matched to HomeView (logo+halo, ~48px title,
 * balanced 480px subtitle), then a soft section anchor and the workspace
 * grid with Workspaces / Recent tabs that mirror the Sessions / Archive
 * pattern in HomeView.
 */
export function LandingPanel({
  workspace,
  workspaces,
  sources,
  tier,
  onTierChange,
  model,
  onModelChange,
  submitting = false,
  onPickWorkspace,
  onSubmit,
  onOpenCreate,
  onOpenConfig,
  onOpenSources,
  onOpenRun,
  onOpenGraph,
}: LandingPanelProps) {
  const [value, setValue] = useState("")
  const [tab, setTab] = useState<Tab>("workspaces")
  const [filter, setFilter] = useState("")

  // The workspaces grid is the scroll target for the "Your workspaces" anchor —
  // mirrors HomeView's chevron→sessions affordance (handleChevronClick). Harmless
  // if the grid is already in view.
  const gridRef = useRef<HTMLDivElement | null>(null)
  const scrollToGrid = useCallback(() => {
    gridRef.current?.scrollIntoView({ behavior: "smooth", block: "start" })
  }, [])

  // Dedupe by normalized query text before slicing so a query run 3× shows ONE
  // chip (not three twins that hover/replay identically); first == most recent.
  const examples = dedupePastQueries(workspace.past_queries ?? []).slice(0, 3)

  // "Recent" surfaces only workspaces with query history, ranked by activity.
  // Backend prepends new past_queries so length is a good recency proxy.
  // The filter input narrows either tab client-side (the server also accepts
  // `?q=` for other clients).
  const sortedWorkspaces = useMemo(() => {
    const base =
      tab === "workspaces"
        ? workspaces
        : workspaces
            .filter((w) => (w.past_queries?.length ?? 0) > 0)
            .sort(
              (a, b) => (b.past_queries?.length ?? 0) - (a.past_queries?.length ?? 0)
            )
    const needle = filter.trim().toLowerCase()
    if (!needle) return base
    return base.filter((w) => matchesWorkspace(w, needle))
  }, [tab, workspaces, filter])

  return (
    <main className="flex-1 overflow-y-auto">
      {/* Hero column — same vertical rhythm as HomeView so the two landings read as one product family. */}
      <section className="mx-auto max-w-[720px] w-full px-4 sm:px-6 flex flex-col items-center text-center pt-[clamp(56px,12vh,140px)] pb-[clamp(32px,6vh,64px)]">
        <img
          src="/logo-transparent.svg"
          alt=""
          aria-hidden
          className="w-14 h-14 mb-5 drop-shadow-[0_0_40px_hsl(var(--primary)/0.30)]"
        />
        <h1 className="text-4xl sm:text-5xl font-semibold tracking-tight mb-2.5 [text-wrap:balance]">
          Agentic search
        </h1>
        <p className="max-w-[480px] mb-6 text-[15px] leading-[1.5] text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
          Ask a question. Sub-agents fan out across your workspace's connected MCPs and bring back ranked results.
        </p>

        <SearchBar
          value={value}
          onChange={setValue}
          onSubmit={onSubmit}
          onReplay={onOpenRun}
          workspace={workspace}
          workspaces={workspaces}
          onPickWorkspace={onPickWorkspace}
          onNewWorkspace={onOpenCreate}
          variant="hero"
          sources={sources}
          onOpenConfig={onOpenConfig}
          tier={tier}
          onTierChange={onTierChange}
          model={model}
          onModelChange={onModelChange}
          submitting={submitting}
          autoFocus
        />

        {workspace.sources.length === 0 && (
          // Pre-submit guard: nothing to fan out across — the view refuses
          // to start a run until at least one source is enabled.
          <div className="mt-3 flex items-center gap-2 text-xs text-[hsl(var(--destructive))]">
            <AlertTriangle className="h-3.5 w-3.5 flex-none" />
            <span>
              This workspace has no sources — searches can't run.{" "}
              <button
                type="button"
                onClick={() => onOpenConfig(workspace)}
                className="underline underline-offset-2 hover:opacity-80"
              >
                Add sources
              </button>
            </span>
          </div>
        )}

        {examples.length > 0 && (
          <div className="mt-6 flex flex-wrap items-center justify-center gap-2 max-w-[640px] px-2">
            {examples.map((e, i) => {
              // A past-query chip REPLAYS its stored run (GET snapshot) when it
              // carries a run_id — it must NOT fire a fresh POST /runs. Only a
              // legacy entry with no run_id falls back to pre-filling a new run.
              // The icon encodes which: History = open the stored run, Search =
              // run this text fresh.
              const replay = Boolean(e.run_id)
              const Icon = replay ? History : Search
              return (
                <button
                  key={pastQueryKey(e, i)}
                  type="button"
                  onClick={() => (e.run_id ? onOpenRun(e.run_id) : onSubmit(e.q))}
                  title={replay ? "Replay this search" : "Search this again"}
                  className="group/chip inline-flex items-center gap-1.5 h-7 max-w-[240px] px-2.5 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-xs text-[hsl(var(--muted-foreground))] shadow-[var(--elev-1)] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] hover:text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] transition-colors"
                >
                  <Icon className="h-3 w-3 flex-none opacity-70 group-hover/chip:opacity-100" />
                  <span className="truncate">{e.q}</span>
                </button>
              )
            })}
          </div>
        )}

        {workspace.sources.length > 0 && (
          // Real health signal for the active workspace — mapped-source
          // coverage, graph size, memory notes — pulled from the existing
          // workspace-graph endpoint (#82). Renders a calm hint, never an error.
          <WorkspaceHealthBand workspace={workspace} onOpenGraph={onOpenGraph} />
        )}
      </section>

      {/* Soft anchor — real scroll affordance mirroring HomeView's "Recent
          sessions ⌄" button: same type size/color, hover-brighten, bounce, and
          a click that scrolls the workspaces grid into view. */}
      <div className="flex justify-center my-2 mb-[clamp(20px,3vw,28px)]">
        <button
          type="button"
          onClick={scrollToGrid}
          aria-label="Scroll to your workspaces"
          className="flex flex-col items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] rounded-md transition-colors animate-scroll-bounce"
        >
          <span>Your workspaces</span>
          <ChevronDown className="h-4 w-4" />
        </button>
      </div>

      {/* Workspaces grid — tabs mirror HomeView's Sessions / Archive treatment. */}
      <div ref={gridRef} className="mx-auto max-w-[1080px] w-full px-4 sm:px-6 pb-20 scroll-mt-4">
        <div className="flex items-center justify-between mb-3.5 gap-3 border-b border-[hsl(var(--border))] pb-2.5">
          <div className="flex gap-6">
            {(["workspaces", "recent"] as const).map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => setTab(t)}
                aria-pressed={tab === t}
                className={cn(
                  "pb-2.5 -mb-[11px] text-sm font-medium border-b-2 transition-colors capitalize cursor-pointer rounded-sm",
                  "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))]",
                  tab === t
                    ? "text-[hsl(var(--foreground))] border-[hsl(var(--foreground))]"
                    : "text-[hsl(var(--muted-foreground))] border-transparent hover:text-[hsl(var(--foreground))]"
                )}
              >
                {t}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-2">
            <div className="relative hidden sm:block">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3 w-3 text-[hsl(var(--muted-foreground))]" />
              <Input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder="Filter workspaces…"
                aria-label="Filter workspaces"
                className="h-7 w-44 pl-7 text-xs"
              />
            </div>
            <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={onOpenSources}>
              <Database className="h-3.5 w-3.5" />
              Sources
            </Button>
            <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={onOpenCreate}>
              <Plus className="h-3.5 w-3.5" />
              New workspace
            </Button>
          </div>
        </div>

        {tab === "recent" && sortedWorkspaces.length === 0 && (
          <div className="py-8 text-center text-sm text-[hsl(var(--muted-foreground))]">
            No searches yet — run one and it'll show up here.
          </div>
        )}

        <div
          className="grid gap-2.5"
          style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}
        >
          {sortedWorkspaces.map((w) => (
            // div+role rather than <button> so the recent-runs popover trigger
            // (a real button) can nest inside without invalid interactive nesting.
            <div
              key={w.id}
              role="button"
              tabIndex={0}
              aria-label={`Open workspace ${w.name}`}
              onClick={() => onPickWorkspace(w)}
              onKeyDown={(e) => {
                if (e.target !== e.currentTarget) return
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault()
                  onPickWorkspace(w)
                }
              }}
              className={cn(
                "group flex flex-col gap-2.5 p-3.5 rounded-xl border text-left min-h-[120px] cursor-pointer",
                "shadow-[var(--elev-1)] hover:shadow-[var(--elev-2)] hover:-translate-y-px",
                "transition-[box-shadow,transform,background-color,border-color] duration-200",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))]",
                w.id === workspace.id
                  ? "border-[hsl(var(--primary)/0.5)] bg-[hsl(var(--primary)/0.04)]"
                  : "border-[hsl(var(--border))] bg-[hsl(var(--card))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)]"
              )}
            >
              {/* Name > description hierarchy; the body claims the slack
                  (`flex-1`) so the meta/action row pins to the card bottom
                  (`mt-auto`) regardless of description length — equal-height
                  grid rows then read as a consistent shelf. */}
              <div className="flex flex-col gap-1 flex-1 min-w-0">
                <h4 className="text-sm font-semibold leading-tight truncate">{w.name}</h4>
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
                    <span className="flex-none whitespace-nowrap text-[10px] font-mono text-[hsl(var(--muted-foreground))] ml-1">
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
                      className="inline-flex items-center justify-center h-6 w-6 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] transition-colors"
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
                      className="inline-flex items-center justify-center h-6 w-6 rounded text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] transition-colors"
                    >
                      <Workflow className="h-3 w-3" />
                    </button>
                  </div>
                  <WorkspaceRunsChip workspace={w} onOpenRun={onOpenRun} />
                </div>
              </div>
            </div>
          ))}
          <button
            type="button"
            aria-label="Create a new workspace"
            onClick={onOpenCreate}
            className="flex flex-col items-center justify-center gap-1.5 p-6 rounded-xl border border-dashed border-[hsl(var(--border-strong))] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent)/0.4)] hover:text-[hsl(var(--foreground))] hover:border-[hsl(var(--primary)/0.5)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] transition-colors min-h-[120px]"
          >
            <Plus className="h-5 w-5" />
            <span className="text-[13px] font-medium">New workspace</span>
            <span className="text-[11px]">Scope MCPs for a topic</span>
          </button>
        </div>
      </div>
    </main>
  )
}

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
 * Active-workspace health band (#82). Reads the workspace SCG graph's stats —
 * mapped-source coverage, graph size (nodes·edges), and memory-note count —
 * via the lightweight `GET /workspaces/<id>/graph/summary` projection (#139),
 * so the landing never downloads the full node/edge graph just to render four
 * numbers (the full graph stays lazy on the dialog). Degrades gracefully: an
 * unmapped / SCG-disabled workspace returns empty stats (every source in
 * `stats.unmapped`), so the band reads "0/N mapped" and links to the map flow
 * rather than erroring.
 */
function WorkspaceHealthBand({
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
      className="mt-5 inline-flex flex-wrap items-center justify-center gap-x-4 gap-y-1.5 px-3.5 py-2 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-xs text-[hsl(var(--muted-foreground))] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] transition-colors"
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
        <span className="inline-flex items-center gap-1 text-[hsl(var(--primary))]">
          <Workflow className="h-3.5 w-3.5" />
          Map sources
        </span>
      )}
    </button>
  )
}

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
function WorkspaceRunsChip({
  workspace,
  onOpenRun,
}: {
  workspace: Workspace
  onOpenRun: (runId: string) => void
}) {
  const [open, setOpen] = useState(false)
  const runsQuery = useWorkspaceRuns(open ? workspace.id : null)
  const runs = (runsQuery.data ?? []).slice(0, 5)

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
          className="inline-flex flex-none items-center gap-1 px-1.5 h-6 rounded whitespace-nowrap text-[11px] font-mono text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--ring))] transition-colors"
        >
          <History className="h-3 w-3 flex-none" />
          {workspace.past_queries?.length ?? 0} past
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-72 p-1 shadow-[var(--elev-3)]"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-2 py-1.5 text-[11px] uppercase tracking-wider text-[hsl(var(--muted-foreground))] font-mono">
          Recent runs
        </div>
        {runsQuery.isPending ? (
          <div className="flex items-center gap-2 px-2 py-2 text-xs text-[hsl(var(--muted-foreground))]">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading runs…
          </div>
        ) : runsQuery.isError ? (
          <div className="px-2 py-2 text-xs text-[hsl(var(--destructive))]">
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
                      "font-mono text-[11px] flex-none w-3 text-center",
                      r.status === "completed" && "text-[hsl(var(--success))]",
                      r.status === "failed" && "text-[hsl(var(--destructive))]",
                      r.status === "running" && "text-[hsl(var(--primary))]"
                    )}
                    title={r.status}
                  >
                    {RUN_STATUS_GLYPH[r.status] ?? "·"}
                  </span>
                  <span className="flex-1 truncate">{r.query}</span>
                  <span
                    className="text-[11px] font-mono text-[hsl(var(--muted-foreground))] flex-none"
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
