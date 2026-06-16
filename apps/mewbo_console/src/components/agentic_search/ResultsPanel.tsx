import { useMemo, useRef, useState } from "react"
import {
  AlertCircle,
  AlertTriangle,
  ArrowUp,
  CircleSlash,
  Clock,
  Code,
  ExternalLink,
  FileText,
  Globe,
  Layers,
  Loader2,
  MessageSquare,
  Shapes,
  Sparkles,
  Target,
  Workflow,
} from "lucide-react"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { cn } from "@/lib/utils"
import { CopyButton } from "../CopyButton"
import type {
  ResultKind,
  SearchResult,
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
import { agentSnapshot, laneSource, runProgress } from "./utils"

/** Next tier up the escalation ladder (fast → auto → deep); null at the top. */
const NEXT_TIER: Record<SearchTier, SearchTier | null> = {
  fast: "auto",
  auto: "deep",
  deep: null,
}

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
  /** Per-run model override ("" = tier default) — session-instance-only. */
  model: string
  onModelChange: (model: string) => void
  /** A new run submission is in flight (mutation pending). */
  submitting?: boolean
  onRun: (query: string) => void
  /** Re-run THIS query one tier up the ladder (fast→auto→deep). Absent at deep. */
  onDeeper?: (query: string, nextTier: SearchTier) => void
  /** Request cancellation of the in-flight run. */
  onCancel?: () => void
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
  model,
  onModelChange,
  submitting = false,
  onRun,
  onDeeper,
  onCancel,
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
  // Per-card follow-up: prefill the composer with the result's context and focus
  // it (reuses the refine prefill+focus path — no separate session continuation;
  // the user edits and submits a fresh run). Honest, minimal context line.
  const handleCardFollowUp = (result: SearchResult) => {
    const ctx = result.url ? `"${result.title}" (${result.url})` : `"${result.title}"`
    setPending(`Regarding ${ctx}: `)
    focusBar()
  }
  // Explicit re-run of THIS exact query (#80): distinct from replaying the
  // stored run — it fires a fresh POST /runs for the same text. Disabled while
  // a submission is already in flight or the workspace has no sources.
  const handleRunAgain = () => {
    if (submitting || workspace.sources.length === 0) return
    submit(run.query)
  }
  // "Go deeper": re-run the same query one tier up (fast→auto→deep). The run's
  // own tier (echoed on the payload) seeds the ladder; hidden at deep.
  const runTier: SearchTier = run.tier ?? tier
  const deeperTier = NEXT_TIER[runTier]
  const handleDeeper = () => {
    if (!deeperTier || submitting || workspace.sources.length === 0) return
    onDeeper?.(run.query, deeperTier)
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
      {/* Top band — one calm sticky search surface in the landing composer's
          visual language: the bar is the focal point (capped + centered like
          the hero), then a single muted meta row. The query appears ONCE, in
          the input — editing/refining is the input's job, not a redundant echo.
          Status lives in exactly one place (the meta row's left), the right
          gathers the calm Copy-link + sources affordances with real spacing. */}
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
              sources={sources}
              onOpenConfig={onOpenConfig}
              tier={tier}
              onTierChange={onTierChange}
              model={model}
              onModelChange={onModelChange}
              submitting={submitting}
              variant="compact"
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
            <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
              <RunStats
                count={visibleResults.length}
                elapsedMs={elapsedMs}
                done={done}
                failed={failed}
                cancelled={cancelled}
              />
              {/* Agent-trace trigger lives in the meta row too, so it's reachable
                  at EVERY width (the right rail — which holds the only at-rest
                  trigger — is hidden below 1100px). */}
              <button
                type="button"
                onClick={() => setTraceOpen(true)}
                className="inline-flex items-center gap-1 hover:text-[hsl(var(--primary))] transition-colors"
              >
                <Layers className="h-3 w-3" />
                Agent trace
                {!done && (
                  <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))] animate-pulse" />
                )}
              </button>
              {/* The backing agent session (deep-dive into the orchestrator's
                  conversation) — only when the BE stamped a session id. */}
              {run.session_id && (
                <a
                  href={`/s/${encodeURIComponent(run.session_id)}`}
                  className="inline-flex items-center gap-1 hover:text-[hsl(var(--primary))] transition-colors"
                >
                  <ExternalLink className="h-3 w-3" />
                  Open agent session
                </a>
              )}
              {/* Steering: cancel an in-flight run. Mirrors the composer Stop —
                  fire-and-forget; the stream's `cancelled` frame flips the view. */}
              {!done && onCancel && (
                <button
                  type="button"
                  onClick={onCancel}
                  className="inline-flex items-center gap-1 text-[hsl(var(--destructive))] hover:opacity-80 transition-opacity"
                >
                  <CircleSlash className="h-3 w-3" />
                  Cancel
                </button>
              )}
              {/* Sources config now lives in the composer's scope control —
                  the band keeps only the share affordance (DRY: status reads
                  in RunStats, config reads in one place). */}
              {run.run_id && (
                <CopyButton
                  text={`${window.location.origin}/search?run=${encodeURIComponent(run.run_id)}`}
                  className="ml-auto h-7 px-2 text-[11px]"
                >
                  Copy link
                </CopyButton>
              )}
            </div>
          </div>
        </div>
      </div>

      {/* Results body — a CONTENT-SIZED two-column grid centered in the
          viewport. The old grid was `minmax(0,1fr) | 270` capped at 1320 with a
          760px main column: the 1fr cell pooled ALL leftover width into a single
          dead gutter between the column and the rail (measured: 242px gap +
          317/331px dead margins → only ~54% of width carried content). Now the
          columns are content-sized (`900px | 340px`) and `justify-center`
          distributes the slack as balanced left/right margins, so the content
          band stays wide and the gutter is just the `gap-x` rhythm. Below
          1100px the rail hides and the grid collapses to one centered column. */}
      <div className="mx-auto max-w-[1320px] px-6 py-4 grid grid-cols-1 justify-center gap-y-4 min-[1100px]:grid-cols-[minmax(0,900px)_340px] min-[1100px]:gap-x-8">
        <div className="min-w-0 w-full">
          {!done && (
            <div className="mb-3">
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
            <div className="mb-4">
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

          <div className="mb-3">
            <FilterRail
              counts={kindCounts}
              active={activeKind}
              onPick={setActiveKind}
            />
          </div>

          <div className="space-y-2">
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
                  onAskFollowUp={handleCardFollowUp}
                />
              )
            })}
            {Array.from({ length: skeletons }).map((_, i) => (
              <ResultSkeleton key={`sk-${i}`} />
            ))}
          </div>

          {done && filtered.length > 0 && (
            <div className="flex items-center gap-3 mt-6 text-xs text-[hsl(var(--muted-foreground))]">
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
              {/* Escalate one tier up the ladder — hidden at the top (deep). */}
              {deeperTier && onDeeper && (
                <>
                  <span aria-hidden>·</span>
                  <button
                    type="button"
                    onClick={handleDeeper}
                    disabled={submitting || workspace.sources.length === 0}
                    className="inline-flex items-center gap-1 hover:text-[hsl(var(--primary))] disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    <ArrowUp className="h-3 w-3" />
                    Go deeper ({deeperTier})
                  </button>
                </>
              )}
              <span className="flex-1 h-px bg-[hsl(var(--border))]" />
            </div>
          )}
        </div>

        <RightRail
          agents={run.trace}
          sources={sources}
          stats={run.stats}
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

interface RunStatsProps {
  count: number
  elapsedMs: number
  done: boolean
  failed: boolean
  cancelled: boolean
}

/**
 * Honest one-line run status (the band's single status readout). The rules:
 *  - While streaming: "streaming · Ns" (live seconds tick via elapsedMs).
 *  - When done: "N results · X.Xs · complete" — but the result count is
 *    SUPPRESSED at 0 (the results column owns the honest empty state) and the
 *    seconds clause is OMITTED when no real elapsed is known. A finished run
 *    must NEVER render "0.0s" — an absent total_ms / 0 elapsed means "duration
 *    unknown", which we show as silence, not a fabricated zero.
 */
function RunStats({ count, elapsedMs, done, failed, cancelled }: RunStatsProps) {
  // Real seconds only when we have a positive elapsed basis (live tick while
  // streaming, or a real total_ms / derived snapshot duration when done).
  const seconds = elapsedMs > 0 ? elapsedMs / 1000 : null

  if (!done) {
    return (
      <span className="inline-flex items-center gap-1.5 tabular-nums">
        <Loader2 className="h-3 w-3 animate-spin text-[hsl(var(--primary))]" />
        <span>streaming</span>
        {seconds != null && (
          <>
            <span aria-hidden>·</span>
            <span>{seconds.toFixed(0)}s</span>
          </>
        )}
      </span>
    )
  }

  const status = failed ? "failed" : cancelled ? "cancelled" : "complete"
  // Build the parts honestly — omit a zero count and an unknown duration.
  const parts: string[] = []
  if (count > 0) parts.push(`${count} ${count === 1 ? "result" : "results"}`)
  if (seconds != null) parts.push(`${seconds.toFixed(1)}s`)
  parts.push(status)

  return (
    <span className="tabular-nums">
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && <span aria-hidden className="mx-1.5">·</span>}
          {p}
        </span>
      ))}
    </span>
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
          const { source: src, isCoordinator } = laneSource(a, sources)
          const count = visibleResults.filter((r) => r.source === a.source_id).length
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
              {/* The coordinator lane (root agent's tool activity, source_id "")
                  has no catalog avatar — render a coordinator glyph instead of a
                  blank SrcAvatar so the lane is never an empty pill. */}
              {isCoordinator ? (
                <Workflow className="h-3.5 w-3.5 opacity-70" />
              ) : (
                <SrcAvatar source={src} size={14} />
              )}
              <span>{src?.name ?? a.name}</span>
              {state === "queued" && <Clock className="h-3 w-3" />}
              {state === "searching" && <Loader2 className="h-3 w-3 animate-spin" />}
              {/* Per-source result count is meaningless for the coordinator
                  (results carry connector source ids, never "") — hide it
                  rather than render a misleading 0. */}
              {state === "done" && !isCoordinator && <span className="font-mono">{count}</span>}
              {state === "empty" && !isCoordinator && <span className="font-mono">0</span>}
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
  // Only render kinds that actually have results (plus "All"). A zero-count chip
  // rendered greyed-out looked identical to a populated one and implied results
  // that aren't there — hiding it removes the dead affordance entirely. The
  // active kind always renders even if a re-filter emptied it, so the user can
  // still see + clear their selection.
  const visibleKinds = KINDS.filter(
    (k) => k.id === "all" || (counts[k.id] ?? 0) > 0 || active === k.id
  )
  // A single populated kind plus "All" is not a meaningful filter — suppress the
  // whole rail rather than show one lonely toggle.
  if (visibleKinds.length <= 2) return null
  return (
    // Wrap instead of horizontal scroll so every kind is visible at rest.
    <div className="flex flex-wrap items-center gap-1.5">
      {visibleKinds.map((k) => {
        const n = counts[k.id] ?? 0
        const isActive = active === k.id
        return (
          <button
            key={k.id}
            type="button"
            onClick={() => onPick(k.id)}
            className={cn(
              "inline-flex items-center gap-1.5 px-3 h-7 rounded-full border text-xs transition-colors",
              isActive
                ? "bg-[hsl(var(--foreground))] text-[hsl(var(--background))] border-[hsl(var(--foreground))]"
                : "border-[hsl(var(--border))] hover:bg-[hsl(var(--accent))]"
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
