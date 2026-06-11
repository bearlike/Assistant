import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { AlertCircle, Loader2 } from "lucide-react"
import { toast } from "sonner"
import { useSearchParams } from "wouter"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import {
  toRunPayload,
  useCreateWorkspace,
  useRun,
  useRunStream,
  useSources,
  useStartRun,
  useUpdateWorkspace,
  useWorkspaces,
} from "../../hooks/useAgenticSearch"
import { useElapsedMs } from "../../hooks/useElapsed"
import type { RunPayload, SearchTier, Workspace, WorkspaceInput } from "../../types/agenticSearch"
import { WorkspaceGraphDialog } from "./graph/WorkspaceGraphDialog"
import { LandingPanel } from "./LandingPanel"
import { ResultsPanel } from "./ResultsPanel"
import { SourcesDialog } from "./SourcesDialog"
import { WorkspaceModal } from "./WorkspaceModal"

const STORAGE_WORKSPACE = "agentic-search:workspace-id"
const STORAGE_TIER = "agentic-search:tier"

const TIER_VALUES: readonly SearchTier[] = ["fast", "auto", "deep"]

function storedTier(): SearchTier {
  if (typeof window === "undefined") return "auto"
  const raw = window.localStorage.getItem(STORAGE_TIER)
  return TIER_VALUES.includes(raw as SearchTier) ? (raw as SearchTier) : "auto"
}

type ModalState = null | { mode: "create" } | { mode: "edit"; workspaceId: string }

/**
 * Page root for the Agentic Search route. Owns transient view state
 * (selected workspace, active run id, modal); all server data flows through
 * useAgenticSearch hooks. Visibility is derived from REAL received stream
 * state — no client-side fake-reveal timer.
 */
export default function AgenticSearchView() {
  const sourcesQuery = useSources()
  const workspacesQuery = useWorkspaces()
  const startRunMutation = useStartRun()
  const createWorkspaceMutation = useCreateWorkspace()
  const updateWorkspaceMutation = useUpdateWorkspace()

  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data])
  const workspaces = useMemo(() => workspacesQuery.data ?? [], [workspacesQuery.data])

  const [modal, setModal] = useState<ModalState>(null)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  // Workspace whose capability graph is open (#79); null = closed.
  const [graphWorkspace, setGraphWorkspace] = useState<Workspace | null>(null)
  // Last-used tier persists like the workspace selection does.
  const [tier, setTier] = useState<SearchTier>(storedTier)

  // URL IS THE SINGLE SOURCE OF TRUTH for {workspace, active run} (#80).
  // Canonical shape: `/search?ws=<workspace_id>&run=<run_id>`. Both facets are
  // DERIVED from the query string — there is no separate `runId`/`workspaceId`
  // useState — so the URL is deterministic, shareable across browsers, and
  // Back/Forward correct (removing `run` closes the run view; removing `ws`
  // falls back to localStorage). localStorage is only the fallback for a bare
  // `/search` visit; a present `ws` param always wins.
  //
  // INERT INVARIANT (#80): a fresh `/search` visit (no `run` param) lands on the
  // inert landing page and NEVER auto-POSTs. The active run is seeded from the
  // `run` param ONLY; opening any `?run=` URL performs GETs only (snapshot +
  // stream attach) — never a `POST /runs`. Replay = GET snapshot, never re-run.
  const [searchParams, setSearchParams] = useSearchParams()
  const runId = searchParams.get("run")
  const wsParam = searchParams.get("ws")

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_TIER, tier)
    }
  }, [tier])

  // Selected workspace id: `ws` param wins; localStorage is the bare-visit
  // fallback. Read lazily once for the fallback so SSR stays safe.
  const storedWorkspaceId =
    typeof window === "undefined" ? null : window.localStorage.getItem(STORAGE_WORKSPACE)
  const workspaceId = wsParam ?? storedWorkspaceId

  // Resolve the current workspace, falling back to the first available one
  // if the selected id is gone or no id has been chosen yet.
  const workspace = useMemo<Workspace | null>(() => {
    if (workspaces.length === 0) return null
    return workspaces.find((w) => w.id === workspaceId) ?? workspaces[0]
  }, [workspaces, workspaceId])

  // Mirror the resolved workspace into localStorage so a later bare `/search`
  // visit (no `ws` param) restores the same selection. The URL param, when
  // present, remains authoritative for the live render.
  useEffect(() => {
    if (!workspace) return
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_WORKSPACE, workspace.id)
    }
  }, [workspace])

  // ── URL writers — every {workspace, run} transition reflects into the URL ──
  // Selecting a workspace is NOT a navigation event → REPLACE the `ws` param.
  const selectWorkspaceParam = useCallback(
    (id: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set("ws", id)
          return next
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  // Opening a run (submit success, replay, runs-chip) IS a navigation event →
  // PUSH `ws`+`run` so browser Back returns to the prior view (landing or run).
  const openRun = useCallback(
    (nextRunId: string, nextWorkspaceId?: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.set("run", nextRunId)
        if (nextWorkspaceId) next.set("ws", nextWorkspaceId)
        return next
      })
    },
    [setSearchParams]
  )

  // Clearing the active run ("Back to search") is a navigation event → PUSH a
  // run-less URL, keeping `ws` so the surrounding workspace context survives.
  const clearRun = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("run")
      return next
    })
  }, [setSearchParams])

  // Live stream (folds the run's SSE event log) + durable snapshot fallback
  // for reload rehydration before the stream has replayed run_started.
  const stream = useRunStream(runId)
  const runQuery = useRun(runId)

  // Real elapsed: ticks while the run is live, freezes on terminal.
  const elapsedMs = useElapsedMs(stream.startedAt, !stream.done)

  // Prefer live stream state once the leg has ATTACHED (≥1 real SSE frame
  // folded); otherwise rehydrate from the snapshot so a reload shows the
  // finished run immediately. `stream.attached` — not `stream.runId` — is the
  // gate: the hook seeds `runId` synchronously on subscribe, so keying off it
  // would render an empty payload before any event arrives. Attaching on the
  // FIRST frame (incl. `agent_start`) is what flips the live view off
  // "Starting search…" even if the `run_started` opener was dropped (#80).
  const run: RunPayload | null = useMemo(() => {
    if (stream.attached) return toRunPayload(stream)
    return runQuery.data?.payload ?? null
  }, [stream, runQuery.data])

  // Done-ness pairs with the AUTHORITATIVE run status, not "rendered from a
  // snapshot ⇒ terminal" (#80). A live stream owns it once attached; before
  // that, the snapshot's own `status` decides — a still-`running` snapshot
  // (an async orchestrated run rehydrated via deep-link before its SSE
  // attaches) must NOT render terminally. Only a genuinely terminal status
  // (completed/failed/cancelled) is `done`.
  const streaming = stream.attached
  const snapshotStatus = runQuery.data?.status
  const snapshotTerminal =
    snapshotStatus === "completed" ||
    snapshotStatus === "failed" ||
    snapshotStatus === "cancelled"
  const done = streaming ? stream.done : snapshotTerminal
  const answerReady = streaming ? stream.answerReady : snapshotTerminal
  const snapshotElapsed = run?.total_ms ?? 0
  const displayElapsed = streaming ? elapsedMs : snapshotElapsed

  // SHARABILITY CORE (#80): a `?run=` URL shared WITHOUT `ws` (or with a
  // mismatched one) must still render the run's own workspace on ANY browser,
  // regardless of the recipient's localStorage. Once the snapshot resolves its
  // `workspace_id` (also carried on the live stream), reconcile `ws` into the
  // URL (REPLACE — this is a derived correction, not a nav event). Reconcile
  // EXACTLY ONCE per run id: after that, a deliberate workspace pick in the
  // results topbar must win — a continuous reconcile would snap it back.
  const resolvedWorkspaceId = stream.attached
    ? stream.workspaceId
    : runQuery.data?.workspace_id ?? null
  const reconciledRunRef = useRef<string | null>(null)
  useEffect(() => {
    if (!runId || !resolvedWorkspaceId) return
    if (reconciledRunRef.current === runId) return
    reconciledRunRef.current = runId
    if (wsParam === resolvedWorkspaceId) return
    selectWorkspaceParam(resolvedWorkspaceId)
  }, [runId, resolvedWorkspaceId, wsParam, selectWorkspaceParam])

  const handleSubmit = (query: string) => {
    if (!workspace) return
    // Pre-submit guard: a workspace with no sources can't fan out — the
    // panels render the inline warning; never POST a doomed run.
    if (workspace.sources.length === 0) return
    if (startRunMutation.isPending) return
    startRunMutation.mutate(
      { workspace_id: workspace.id, query, tier },
      {
        onSuccess: (res) => {
          // The run id arrives async from the POST — PUSH it (with the run's
          // workspace) into the URL so the result is shareable and Back returns
          // to the landing. This REPLACES any stale `run` param in place.
          openRun(res.run_id, workspace.id)
        },
      }
    )
  }

  // Opening a stored run from any surface (replay chip, runs popover,
  // results-rail). Idempotent GET-only rehydration — never a POST (#80).
  const handleOpenRun = useCallback(
    (nextRunId: string) => {
      openRun(nextRunId)
    },
    [openRun]
  )

  // ROUND-TRIP FIX #2: switching workspaces changes selection ONLY (REPLACE the
  // `ws` param). It must not auto-re-run the last query — the user submits
  // explicitly.
  const handlePickWorkspace = (next: Workspace) => {
    selectWorkspaceParam(next.id)
  }

  const handleSaveWorkspace = (values: WorkspaceInput) => {
    if (modal?.mode === "edit") {
      // A graph-lifecycle edit (#83): changing the purpose/instructions/desc or
      // the source selection re-indexes the workspace's capability graph. Compare
      // against the prior state so an unrelated edit (e.g. just the name) stays
      // quiet — the smallest honest signal that a re-index was kicked off.
      const prior = workspaces.find((w) => w.id === modal.workspaceId)
      const reindexed =
        prior != null &&
        (prior.instructions !== values.instructions ||
          prior.desc !== values.desc ||
          prior.sources.join(" ") !== values.sources.join(" "))
      updateWorkspaceMutation.mutate(
        { id: modal.workspaceId, input: values },
        {
          onSuccess: (updated) => {
            selectWorkspaceParam(updated.id)
            setModal(null)
            if (reindexed) {
              toast.success("Re-indexing the workspace graph", {
                description: "Mapped sources re-index in the background.",
              })
            }
          },
        }
      )
    } else if (modal?.mode === "create") {
      createWorkspaceMutation.mutate(values, {
        onSuccess: (created) => {
          selectWorkspaceParam(created.id)
          setModal(null)
        },
      })
    }
  }

  const editingWorkspace =
    modal?.mode === "edit"
      ? workspaces.find((w) => w.id === modal.workspaceId) ?? null
      : null
  const modalSubmitting =
    createWorkspaceMutation.isPending || updateWorkspaceMutation.isPending

  // Pending server state renders as a landing-shaped skeleton (hero rhythm +
  // workspace-card placeholders) so the real grid replaces it in place
  // instead of popping in after a blank spinner screen.
  if (sourcesQuery.isPending || workspacesQuery.isPending) {
    return <LandingSkeleton />
  }
  if (sourcesQuery.isError || workspacesQuery.isError) {
    return (
      <div className="flex-1 flex items-center justify-center p-6 text-center">
        <div>
          <div className="text-sm font-medium text-[hsl(var(--destructive))]">
            Couldn't reach the search API.
          </div>
          <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
            Check that the Mewbo API server is running and the master token is set.
          </p>
        </div>
      </div>
    )
  }
  if (!workspace) {
    // First-run empty state: no workspaces exist yet.
    return (
      <>
        <div className="flex-1 flex items-center justify-center p-6 text-center">
          <div className="max-w-[360px]">
            <div className="text-sm font-medium">No workspaces yet</div>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))] [text-wrap:balance]">
              A workspace groups the MCP sources a search can fan out across.
              Create one to run your first search.
            </p>
            <Button variant="primary" className="mt-4" onClick={() => setModal({ mode: "create" })}>
              Create your first workspace
            </Button>
          </div>
        </div>
        <WorkspaceModal
          open={modal !== null}
          initial={null}
          sources={sources}
          onClose={() => setModal(null)}
          onSubmit={handleSaveWorkspace}
          submitting={modalSubmitting}
        />
      </>
    )
  }

  // A run id is active but neither the stream nor the snapshot has produced
  // renderable state yet (submit → run_started gap, or reload rehydration).
  const awaitingRun = Boolean(runId) && !run
  const submitting = startRunMutation.isPending

  return (
    <>
      {run ? (
        <ResultsPanel
          workspace={workspace}
          workspaces={workspaces}
          sources={sources}
          query={run.query}
          run={run}
          elapsedMs={displayElapsed}
          done={done}
          answerReady={answerReady}
          isLoading={submitting || (Boolean(runId) && runQuery.isLoading && !stream.attached)}
          submitting={submitting}
          tier={tier}
          onTierChange={setTier}
          onRun={handleSubmit}
          onOpenRun={handleOpenRun}
          onOpenGraph={() => setGraphWorkspace(workspace)}
          onPickWorkspace={handlePickWorkspace}
          onOpenCreate={() => setModal({ mode: "create" })}
          onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
        />
      ) : awaitingRun && runQuery.isError && !submitting ? (
        // The snapshot fetch failed and no live stream exists — surface it
        // instead of silently falling back to the landing page.
        <div className="flex-1 flex items-center justify-center p-6">
          <Alert variant="destructive" className="max-w-md">
            <AlertCircle className="h-4 w-4" />
            <AlertTitle>Couldn't load that run</AlertTitle>
            <AlertDescription>
              {runQuery.error instanceof Error
                ? runQuery.error.message
                : "The run snapshot could not be fetched."}
              <div className="mt-3">
                <Button variant="neutral" size="sm" onClick={clearRun}>
                  Back to search
                </Button>
              </div>
            </AlertDescription>
          </Alert>
        </div>
      ) : awaitingRun ? (
        // In-flight: the run was accepted (or is being rehydrated) but no
        // run_started / snapshot has landed yet. Real state, not a timer.
        <div className="flex-1 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-sm">
          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
          Starting search…
        </div>
      ) : (
        <LandingPanel
          workspace={workspace}
          workspaces={workspaces}
          sources={sources}
          tier={tier}
          onTierChange={setTier}
          submitting={submitting}
          onPickWorkspace={handlePickWorkspace}
          onSubmit={handleSubmit}
          onOpenCreate={() => setModal({ mode: "create" })}
          onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
          onOpenSources={() => setSourcesOpen(true)}
          onOpenRun={handleOpenRun}
          onOpenGraph={setGraphWorkspace}
        />
      )}

      {startRunMutation.isError && (
        <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 px-4 py-2 rounded-md bg-[hsl(var(--destructive))] text-[hsl(var(--destructive-foreground))] text-sm shadow-lg">
          Search failed: {startRunMutation.error?.message ?? "unknown error"}
        </div>
      )}

      <WorkspaceModal
        open={modal !== null}
        initial={editingWorkspace}
        sources={sources}
        onClose={() => setModal(null)}
        onSubmit={handleSaveWorkspace}
        submitting={modalSubmitting}
      />

      <SourcesDialog
        open={sourcesOpen}
        sources={sources}
        onClose={() => setSourcesOpen(false)}
      />

      {graphWorkspace && (
        <WorkspaceGraphDialog
          open={graphWorkspace !== null}
          workspace={graphWorkspace}
          onClose={() => setGraphWorkspace(null)}
          onMapSource={() => {
            setGraphWorkspace(null)
            setSourcesOpen(true)
          }}
        />
      )}
    </>
  )
}

/** One pulsing placeholder line — the subsystem's shared skeleton idiom
 *  (same classes as `AnswerCard.SkeletonLine` / `ResultsPanel.ResultSkeleton`). */
function SkeletonLine({ className }: { className: string }) {
  return <div className={cn("rounded bg-[hsl(var(--muted))] animate-pulse", className)} />
}

/**
 * Pending state for the workspace/source queries — mirrors the landing
 * layout (hero column + workspace grid) with pulsing placeholders so loaded
 * content replaces it in place rather than popping in.
 */
function LandingSkeleton() {
  return (
    <main className="flex-1 overflow-y-auto" aria-busy="true" aria-label="Loading workspaces">
      <section className="mx-auto max-w-[720px] w-full px-4 sm:px-6 flex flex-col items-center pt-[clamp(56px,12vh,140px)] pb-[clamp(32px,6vh,64px)]">
        <SkeletonLine className="w-14 h-14 mb-5 rounded-full" />
        <SkeletonLine className="h-9 w-64 mb-3" />
        <SkeletonLine className="h-4 w-80 mb-6" />
        <SkeletonLine className="w-full max-w-[720px] min-h-[96px] rounded-2xl border border-[hsl(var(--border))] bg-[hsl(var(--card))]" />
      </section>
      <div className="mx-auto max-w-[1080px] w-full px-4 sm:px-6 pb-20">
        <div
          className="grid gap-2.5"
          style={{ gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))" }}
        >
          {Array.from({ length: 4 }).map((_, i) => (
            <div
              key={i}
              className="flex flex-col gap-2.5 p-3.5 rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] min-h-[120px]"
            >
              <SkeletonLine className="h-4 w-2/3" />
              <SkeletonLine className="h-3 w-4/5" />
              <SkeletonLine className="mt-auto h-5 w-24" />
            </div>
          ))}
        </div>
      </div>
    </main>
  )
}
