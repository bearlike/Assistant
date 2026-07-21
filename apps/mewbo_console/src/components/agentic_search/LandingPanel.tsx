import { useCallback, useMemo, useRef, useState } from "react"
import {
  AlertTriangle,
  ChevronDown,
  Database,
  History,
  Plus,
  Search,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { FOCUS_RING } from "@/components/ui/focus-ring"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { ProductHero } from "../ProductHero"
import type { SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { SearchBar } from "./SearchBar"
import { type SearchScope } from "./SearchScopeControl"
import { dedupePastQueries, pastQueryKey } from "./utils"
import { WorkspaceCard } from "./WorkspaceCard"
import { WorkspaceHealthBand } from "./WorkspaceHealthBand"

interface LandingPanelProps {
  workspace: Workspace
  workspaces: Workspace[]
  sources: SourceCatalogEntry[]
  /** Budget tier + model override, bundled with their setters. */
  scope: SearchScope
  /** A run submission is in flight (mutation pending). */
  submitting?: boolean
  onSelectWorkspace: (workspace: Workspace) => void
  onSubmit: (query: string) => void
  onOpenCreate: () => void
  onOpenConfig: (workspace: Workspace) => void
  onOpenSources: () => void
  /** Open a past run by id (rehydrates via the run snapshot / stream). */
  onOpenRun: (runId: string) => void
  /** Open a workspace's capability graph. */
  onOpenGraph: (workspace: Workspace) => void
}

type Tab = "workspaces" | "recent"

/** Past-query example chips shown under the hero composer. */
const MAX_EXAMPLE_CHIPS = 3

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
  scope,
  submitting = false,
  onSelectWorkspace,
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
  const examples = dedupePastQueries(workspace.past_queries ?? []).slice(0, MAX_EXAMPLE_CHIPS)

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
    <div className="flex-1 overflow-y-auto">
      <ProductHero
        title="Agentic Search"
        subtitle="Ask a question. Sub-agents fan out across your workspace's connected MCPs and bring back ranked results."
      >
        <SearchBar
          value={value}
          onChange={setValue}
          onSubmit={onSubmit}
          onReplay={onOpenRun}
          workspace={workspace}
          workspaces={workspaces}
          onSelectWorkspace={onSelectWorkspace}
          onNewWorkspace={onOpenCreate}
          variant="hero"
          sources={sources}
          onOpenConfig={onOpenConfig}
          scope={scope}
          submitting={submitting}
          autoFocus
        />

        {workspace.sources.length === 0 && (
          // Pre-submit guard: nothing to fan out across — the view refuses
          // to start a run until at least one source is enabled.
          <div className="mt-3 flex items-center gap-2 text-xs text-[hsl(var(--destructive-text))]">
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
                  className={`group/chip inline-flex items-center gap-1.5 h-7 max-w-[240px] px-2.5 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-xs text-[hsl(var(--muted-foreground))] [box-shadow:var(--elev-1)] hover:border-[hsl(var(--primary)/0.4)] hover:bg-[hsl(var(--accent)/0.4)] hover:text-[hsl(var(--foreground))] ${FOCUS_RING} transition-colors`}
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
          // workspace-graph endpoint. Renders a calm hint, never an error.
          <WorkspaceHealthBand workspace={workspace} onOpenGraph={onOpenGraph} />
        )}
      </ProductHero>

      {/* Soft anchor — real scroll affordance mirroring HomeView's "Recent
          sessions ⌄" button: same type size/color, hover-brighten, bounce, and
          a click that scrolls the workspaces grid into view. */}
      <div className="flex justify-center my-2 mb-[clamp(20px,3vw,28px)]">
        <button
          type="button"
          onClick={scrollToGrid}
          aria-label="Scroll to your workspaces"
          className={`flex flex-col items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] ${FOCUS_RING} rounded-md transition-colors animate-scroll-bounce`}
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
                  FOCUS_RING,
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
                className="h-7 w-44 pl-6"
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
            <WorkspaceCard
              key={w.id}
              workspace={w}
              active={w.id === workspace.id}
              sources={sources}
              onSelect={onSelectWorkspace}
              onOpenConfig={onOpenConfig}
              onOpenGraph={onOpenGraph}
              onOpenRun={onOpenRun}
            />
          ))}
          <button
            type="button"
            aria-label="Create a new workspace"
            onClick={onOpenCreate}
            className={`flex flex-col items-center justify-center gap-1.5 p-6 rounded-xl border border-dashed border-[hsl(var(--border-strong))] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent)/0.4)] hover:text-[hsl(var(--foreground))] hover:border-[hsl(var(--primary)/0.5)] ${FOCUS_RING} transition-colors min-h-[120px]`}
          >
            <Plus className="h-5 w-5" />
            <span className="text-sm font-medium">New workspace</span>
            <span className="text-2xs">Scope MCPs for a topic</span>
          </button>
        </div>
      </div>
    </div>
  )
}
