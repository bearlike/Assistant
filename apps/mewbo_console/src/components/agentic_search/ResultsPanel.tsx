import { useMemo, useRef, useState } from "react"
import {
  AlertCircle,
  AlertTriangle,
  CircleSlash,
  Clock,
  Code,
  FileText,
  Globe,
  Loader2,
  MessageSquare,
  Shapes,
  SlidersHorizontal,
  Sparkles,
  Target,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { cn } from "@/lib/utils"
import { CopyButton } from "../CopyButton"
import type {
  ResultKind,
  RunPayload,
  SearchTier,
  SourceCatalogEntry,
  Workspace,
} from "../../types/agenticSearch"
import { AnswerCard } from "./AnswerCard"
import { ResultCard } from "./ResultCard"
import { RightRail } from "./RightRail"
import { SearchBar } from "./SearchBar"
import { SrcAvatar } from "./SrcAvatar"
import { TraceDrawer } from "./TraceDrawer"
import { agentSnapshot, runProgress } from "./utils"

const KINDS: { id: "all" | ResultKind; name: string; Icon: typeof Sparkles }[] = [
  { id: "all", name: "All", Icon: Sparkles },
  { id: "docs", name: "Docs", Icon: FileText },
  { id: "code", name: "Code", Icon: Code },
  { id: "threads", name: "Threads", Icon: MessageSquare },
  { id: "design", name: "Design", Icon: Shapes },
  { id: "tickets", name: "Tickets", Icon: Target },
  { id: "web", name: "Web", Icon: Globe },
]

interface ResultsPanelProps {
  workspace: Workspace
  workspaces: Workspace[]
  sources: SourceCatalogEntry[]
  query: string
  run: RunPayload
  /** Real elapsed ms since run start (display + status line). */
  elapsedMs: number
  /** Run reached a terminal state (drives "complete" vs "streaming"). */
  done: boolean
  /** Final cited synthesis has landed (`answer_ready`). */
  answerReady: boolean
  isLoading: boolean
  tier: SearchTier
  onTierChange: (tier: SearchTier) => void
  /** A new run submission is in flight (mutation pending). */
  submitting?: boolean
  onRun: (query: string) => void
  /** Replay a stored run by id (GET snapshot) — past-query suggestions use
   *  this instead of re-running. */
  onOpenRun?: (runId: string) => void
  /** Open the workspace's capability graph (#79). */
  onOpenGraph?: () => void
  onPickWorkspace: (workspace: Workspace) => void
  onOpenCreate: () => void
  onOpenConfig: (workspace: Workspace) => void
}

export function ResultsPanel({
  workspace,
  workspaces,
  sources,
  query,
  run,
  elapsedMs,
  done,
  answerReady,
  isLoading,
  tier,
  onTierChange,
  submitting = false,
  onRun,
  onOpenRun,
  onOpenGraph,
  onPickWorkspace,
  onOpenCreate,
  onOpenConfig,
}: ResultsPanelProps) {
  const [pending, setPending] = useState(query)
  const [traceOpen, setTraceOpen] = useState(false)
  const [activeKind, setActiveKind] = useState<"all" | ResultKind>("all")
  const [expandedId, setExpandedId] = useState<string | null>(null)
  const [highlightId, setHighlightId] = useState<string | null>(null)
  const barRef = useRef<HTMLDivElement | null>(null)

  // Run lifecycle, straight off the folded stream / snapshot state.
  const failed = run.status === "failed"
  const cancelled = run.status === "cancelled"
  const hasAnswerContent =
    run.answer.tldr.length > 0 || run.answer.bullets.length > 0

  const focusBar = () => {
    barRef.current?.querySelector<HTMLInputElement>("input")?.focus()
  }
  // Follow-up keeps the workspace and clears the bar for a fresh question;
  // refine pre-fills the bar with the run's query for editing. Both submit
  // through the existing run-start path (runs are independent — no session
  // continuation on the server contract).
  const handleFollowUp = () => {
    setPending("")
    focusBar()
  }
  const handleRefine = () => {
    setPending(run.query)
    focusBar()
  }
  // Explicit re-run of THIS exact query (#80): distinct from replaying the
  // stored run — it fires a fresh POST /runs for the same text. Disabled while
  // a submission is already in flight or the workspace has no sources.
  const handleRunAgain = () => {
    if (submitting || workspace.sources.length === 0) return
    submit(run.query)
  }

  // Every result in `run.results` has already arrived over SSE — visibility is
  // the full set, no fake `elapsed`-based reveal. While agents are still
  // running we show a couple of skeleton cards as a streaming affordance.
  //
  // Belt-and-suspenders dedup by unique result id (#82): the stream reducer
  // already drops duplicate `result` ids, but a snapshot↔SSE merge (or a
  // backend echo replay) could still hand a list with repeats. Two cards
  // sharing an id render the same React key AND the same `result-<id>` DOM id —
  // that id collision is what makes hovering one "light up" its twin. Keep the
  // FIRST occurrence so identity is strictly per-id.
  const visibleResults = useMemo(() => {
    const seen = new Set<string>()
    return run.results.filter((r) => {
      if (seen.has(r.id)) return false
      seen.add(r.id)
      return true
    })
  }, [run.results])
  const runningAgents = run.trace.filter((a) => agentSnapshot(a).running).length
  const skeletons = done ? 0 : Math.min(2, Math.max(runningAgents, run.trace.length === 0 ? 1 : 0))

  const kindCounts = useMemo(() => {
    const c: Record<string, number> = { all: visibleResults.length }
    for (const r of visibleResults) c[r.kind] = (c[r.kind] ?? 0) + 1
    return c
  }, [visibleResults])

  const filtered = activeKind === "all"
    ? visibleResults
    : visibleResults.filter((r) => r.kind === activeKind)

  const handleCite = (rid: string) => {
    setExpandedId(rid)
    setHighlightId(rid)
    window.setTimeout(() => {
      const el = document.getElementById(`result-${rid}`)
      el?.scrollIntoView({ behavior: "smooth", block: "center" })
    }, 50)
    window.setTimeout(() => setHighlightId(null), 1400)
  }

  const submit = (q: string) => {
    setPending(q)
    onRun(q)
  }

  return (
    <main className="flex-1 overflow-y-auto">
      {/* Sub-bar surface — full-width sticky band with hairline divider.
          Page container matches the mockup at max-w 1320 with 24px padding;
          the search bar stays capped at 570px and centered as a focal point. */}
      <div className="sticky top-0 z-10 bg-[hsl(var(--background)/0.92)] backdrop-blur-md border-b border-[hsl(var(--border))]">
        <div className="mx-auto max-w-[1320px] px-6 py-3">
          <div ref={barRef} className="mx-auto max-w-[570px]">
            <SearchBar
              value={pending}
              onChange={setPending}
              onSubmit={submit}
              onReplay={onOpenRun}
              workspace={workspace}
              workspaces={workspaces}
              onPickWorkspace={onPickWorkspace}
              onNewWorkspace={onOpenCreate}
              tier={tier}
              onTierChange={onTierChange}
              submitting={submitting}
              compact
            />
            {workspace.sources.length === 0 && (
              <div className="mt-2 flex items-center gap-2 text-xs text-[hsl(var(--destructive))]">
                <AlertTriangle className="h-3.5 w-3.5 flex-none" />
                <span>
                  This workspace has no sources — new searches can't run.{" "}
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
            <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] italic font-mono text-[hsl(var(--muted-foreground))]">
              <span className="font-medium text-[hsl(var(--foreground))] truncate max-w-[40%]">
                "{run.query}"
              </span>
              <span aria-hidden>·</span>
              <span>
                <b className="text-[hsl(var(--foreground))] font-medium">{visibleResults.length}</b>
                {" "}results
              </span>
              <span aria-hidden>·</span>
              <span>
                {(elapsedMs / 1000).toFixed(1)}s ·{" "}
                {!done ? "streaming" : failed ? "failed" : cancelled ? "cancelled" : "complete"}
              </span>
              {run.run_id && (
                <CopyButton
                  text={`${window.location.origin}/search?run=${encodeURIComponent(run.run_id)}`}
                  className="not-italic h-7 px-2 text-[11px] font-mono"
                >
                  Copy link
                </CopyButton>
              )}
              <button
                type="button"
                onClick={() => onOpenConfig(workspace)}
                className="ml-auto not-italic inline-flex items-center gap-1.5 px-2 h-7 rounded hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
              >
                <SlidersHorizontal className="h-3 w-3" />
                <span>{workspace.sources.length} sources</span>
              </button>
            </div>
          </div>
        </div>
      </div>

      {/* Results body — mockup grid (styles.css `.results-body`):
          1320 outer cap, two columns `minmax(0,1fr) | 270`, gap 32. The main
          column is capped at 760px per the mockup so it never stretches edge
          to edge — the 1fr cell gathers the leftover space as a visible
          margin between the synthesis column and the agent-trace rail.
          Below 1100px the rail hides and the grid collapses to one column. */}
      <div className="mx-auto max-w-[1320px] px-6 py-6 grid grid-cols-1 gap-y-6 min-[1100px]:grid-cols-[minmax(0,1fr)_270px] min-[1100px]:gap-x-8">
        <div className="min-w-0 max-w-[760px] w-full">
          {!done && (
            <div className="mb-4">
              <ProgressStrip
                agents={run.trace}
                sources={sources}
                visibleResults={visibleResults}
              />
            </div>
          )}
          {isLoading && (
            <div className="mb-3 text-xs text-[hsl(var(--muted-foreground))] flex items-center gap-2">
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
              Loading run…
            </div>
          )}

          {/* Terminal edge states, straight off run.status / run.error. */}
          {failed && (
            <Alert variant="destructive" className="mb-4">
              <AlertCircle className="h-4 w-4" />
              <AlertTitle>Search failed</AlertTitle>
              <AlertDescription>
                {run.error || "The run ended with an error before completing."}
              </AlertDescription>
            </Alert>
          )}
          {cancelled && (
            <div className="mb-4 flex items-center gap-2 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted)/0.4)] px-4 py-3 text-sm text-[hsl(var(--muted-foreground))]">
              <CircleSlash className="h-4 w-4 flex-none" />
              Search was cancelled. Results below are partial.
            </div>
          )}

          {/* The synthesis card renders only when there is (or will be) answer
              content — a run that died before any answer_delta shows its
              terminal state above instead of a forever-pulsing skeleton. */}
          {(!done || hasAnswerContent) && (
            <div className="mb-6">
              <AnswerCard
                answer={run.answer}
                results={visibleResults}
                sources={sources}
                ready={answerReady}
                done={done}
                elapsedMs={elapsedMs}
                onCiteClick={handleCite}
                onAsk={handleFollowUp}
              />
            </div>
          )}

          <div className="mb-4">
            <FilterRail
              counts={kindCounts}
              active={activeKind}
              onPick={setActiveKind}
            />
          </div>

          <div className="space-y-3">
            {/* Deliberate zero-results state for a finished run. Failed and
                cancelled runs surface their own terminal blocks above. */}
            {done && !failed && !cancelled && visibleResults.length === 0 && (
              <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-4 py-6 text-center text-sm text-[hsl(var(--muted-foreground))]">
                <div className="font-medium text-[hsl(var(--foreground))]">No results</div>
                <p className="mt-1 text-xs [text-wrap:balance]">
                  None of this workspace's sources returned a match for this query.
                </p>
                <button
                  type="button"
                  onClick={handleRefine}
                  className="mt-3 text-[hsl(var(--primary))] hover:underline text-xs"
                >
                  Refine query
                </button>
              </div>
            )}
            {filtered.length === 0 && visibleResults.length > 0 && (
              <div className="text-sm text-[hsl(var(--muted-foreground))] py-4 text-center">
                No <b>{KINDS.find((k) => k.id === activeKind)?.name}</b> results in this query.{" "}
                <button
                  type="button"
                  onClick={() => setActiveKind("all")}
                  className="text-[hsl(var(--primary))] hover:underline"
                >
                  Show all
                </button>
              </div>
            )}
            {filtered.map((r) => {
              const num = visibleResults.findIndex((x) => x.id === r.id) + 1
              return (
                <ResultCard
                  key={r.id}
                  result={r}
                  num={num}
                  expanded={expandedId === r.id}
                  highlighted={highlightId === r.id}
                  sources={sources}
                  onToggle={() => setExpandedId(expandedId === r.id ? null : r.id)}
                />
              )
            })}
            {Array.from({ length: skeletons }).map((_, i) => (
              <ResultSkeleton key={`sk-${i}`} />
            ))}
          </div>

          {done && filtered.length > 0 && (
            <div className="flex items-center gap-3 mt-8 text-xs text-[hsl(var(--muted-foreground))]">
              <span className="flex-1 h-px bg-[hsl(var(--border))]" />
              <span>End of results</span>
              <span aria-hidden>·</span>
              <button type="button" onClick={handleFollowUp} className="hover:text-[hsl(var(--primary))]">
                Ask a follow-up
              </button>
              <span aria-hidden>·</span>
              <button type="button" onClick={handleRefine} className="hover:text-[hsl(var(--primary))]">
                Refine query
              </button>
              <span aria-hidden>·</span>
              <button
                type="button"
                onClick={handleRunAgain}
                disabled={submitting || workspace.sources.length === 0}
                className="hover:text-[hsl(var(--primary))] disabled:opacity-50 disabled:cursor-not-allowed"
              >
                Run again
              </button>
              <span className="flex-1 h-px bg-[hsl(var(--border))]" />
            </div>
          )}
        </div>

        <RightRail
          agents={run.trace}
          sources={sources}
          related={run.related_questions}
          people={run.related_people}
          done={done}
          traceActive={!done}
          onShowTrace={() => setTraceOpen(true)}
          onAsk={(q) => submit(q)}
          onShowGraph={onOpenGraph}
        />
      </div>

      <TraceDrawer
        open={traceOpen}
        onOpenChange={setTraceOpen}
        agents={run.trace}
        query={run.query}
        elapsedMs={elapsedMs}
        done={done}
      />
    </main>
  )
}

interface ProgressStripProps {
  agents: RunPayload["trace"]
  sources: SourceCatalogEntry[]
  visibleResults: RunPayload["results"]
}

function ProgressStrip({ agents, sources, visibleResults }: ProgressStripProps) {
  // Real progress: fraction of spawned agents that have finished. This strip
  // only mounts mid-run (`!done`), so the bar is always in its streaming state.
  const progress = runProgress(agents, false)
  return (
    <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 shadow-[var(--elev-1)]">
      <div className="h-0.5 w-full bg-[hsl(var(--muted))] relative rounded-full overflow-hidden mb-2.5">
        <span
          className="absolute inset-y-0 left-0 bg-[hsl(var(--primary))] transition-[width]"
          style={{ width: `${progress * 100}%` }}
        />
      </div>
      <div className="flex flex-wrap gap-1.5">
        {agents.map((a) => {
          const { state } = agentSnapshot(a)
          const count = visibleResults.filter((r) => r.source === a.source_id).length
          const src = sources.find((s) => s.id === a.source_id)
          return (
            <div
              key={a.id}
              className={cn(
                "inline-flex items-center gap-1.5 px-2 py-1 rounded-full border text-[11px] font-medium",
                state === "queued" &&
                  "border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] opacity-60",
                state === "searching" &&
                  "border-[hsl(var(--primary)/0.4)] bg-[hsl(var(--primary)/0.06)] text-[hsl(var(--primary))]",
                state === "done" &&
                  "border-[hsl(var(--success)/0.4)] bg-[hsl(var(--success)/0.06)] text-[hsl(var(--success))]",
                state === "empty" &&
                  "border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] opacity-60"
              )}
            >
              <SrcAvatar source={src} size={14} />
              <span>{src?.name ?? a.name}</span>
              {state === "queued" && <Clock className="h-3 w-3" />}
              {state === "searching" && <Loader2 className="h-3 w-3 animate-spin" />}
              {state === "done" && <span className="font-mono">{count}</span>}
              {state === "empty" && <span className="font-mono">0</span>}
            </div>
          )
        })}
      </div>
    </div>
  )
}

interface FilterRailProps {
  counts: Record<string, number>
  active: "all" | ResultKind
  onPick: (id: "all" | ResultKind) => void
}

function FilterRail({ counts, active, onPick }: FilterRailProps) {
  return (
    // Wrap instead of horizontal scroll so every kind is visible at rest.
    // Once the column is wide enough (most desktops) all 7 stay on one line;
    // on narrower viewports the row wraps gracefully to a second line.
    <div className="flex flex-wrap items-center gap-1.5">
      {KINDS.map((k) => {
        const n = counts[k.id] ?? 0
        const disabled = k.id !== "all" && n === 0
        const isActive = active === k.id
        return (
          <button
            key={k.id}
            type="button"
            disabled={disabled}
            onClick={() => onPick(k.id)}
            className={cn(
              "inline-flex items-center gap-1.5 px-3 h-7 rounded-full border text-xs transition-colors",
              isActive
                ? "bg-[hsl(var(--foreground))] text-[hsl(var(--background))] border-[hsl(var(--foreground))]"
                : "border-[hsl(var(--border))] hover:bg-[hsl(var(--accent))]",
              disabled && "opacity-40 cursor-not-allowed hover:bg-transparent"
            )}
          >
            <k.Icon className="h-3 w-3" />
            <span>{k.name}</span>
            {n > 0 && (
              <span className="font-mono text-[10px] opacity-70">{n}</span>
            )}
          </button>
        )
      })}
    </div>
  )
}

function ResultSkeleton() {
  return (
    <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 shadow-[var(--elev-1)] space-y-2">
      <div className="h-3 w-1/3 rounded bg-[hsl(var(--muted))] animate-pulse" />
      <div className="h-4 w-4/5 rounded bg-[hsl(var(--muted))] animate-pulse" />
      <div className="h-3 w-3/5 rounded bg-[hsl(var(--muted))] animate-pulse" />
    </div>
  )
}
