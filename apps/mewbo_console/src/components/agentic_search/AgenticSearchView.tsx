import { lazy, Suspense, useCallback, useMemo, useState } from "react"
import { AlertCircle, Loader2 } from "lucide-react"
import { toast } from "sonner"

import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { cn } from "@/lib/utils"

import {
  useCancelRun,
  useCreateWorkspace,
  useSources,
  useStartRun,
  useUpdateWorkspace,
  useWorkspaces,
} from "../../hooks/useAgenticSearch"
import { useResolvedRun } from "../../hooks/useResolvedRun"
import {
  storedTier,
  useReconcileWorkspaceParam,
  useSearchUrlState,
} from "../../hooks/useSearchUrlState"
import type {
  SearchTier,
  Workspace,
  WorkspaceInput,
} from "../../types/agenticSearch"
import { LandingPanel } from "./LandingPanel"
import { type SearchScope } from "./SearchScopeControl"
import { SourcesDialog } from "./SourcesDialog"
import { WorkspaceModal } from "./WorkspaceModal"

// Run + graph surfaces are code-split so the inert landing page (the common
// first paint) never downloads the heavy chunks they pull in: ResultsPanel →
// AnswerCard → react-markdown/rehype-highlight, and WorkspaceGraphDialog →
// Graph3DView (react-force-graph-3d + three.js). They only mount on an active
// run or when a user opens the graph, so deferring their import is free.
const ResultsPanel = lazy(() =>
  import("./ResultsPanel").then((m) => ({ default: m.ResultsPanel }))
)
const WorkspaceGraphDialog = lazy(() =>
  import("./graph/WorkspaceGraphDialog").then((m) => ({ default: m.WorkspaceGraphDialog }))
)

type ModalState = null | { mode: "create" } | { mode: "edit"; workspaceId: string }

/**
 * Page root for the Agentic Search route. Owns transient view state
 * (selected workspace, active run id, modal); all server data flows through
 * useAgenticSearch hooks. Visibility is derived from REAL received stream
 * state — no client-side fake-reveal timer.
 */
export function AgenticSearchView() {
  const sourcesQuery = useSources()
  const workspacesQuery = useWorkspaces()
  const startRunMutation = useStartRun()
  const cancelRunMutation = useCancelRun()
  const createWorkspaceMutation = useCreateWorkspace()
  const updateWorkspaceMutation = useUpdateWorkspace()

  const sources = useMemo(() => sourcesQuery.data ?? [], [sourcesQuery.data])
  const workspaces = useMemo(() => workspacesQuery.data ?? [], [workspacesQuery.data])

  const [modal, setModal] = useState<ModalState>(null)
  const [sourcesOpen, setSourcesOpen] = useState(false)
  // Workspace whose capability graph is open; null = closed.
  const [graphWorkspace, setGraphWorkspace] = useState<Workspace | null>(null)
  // Last-used tier persists like the workspace selection does.
  const [tier, setTier] = useState<SearchTier>(storedTier)
  // Per-run model override ("" = the tier's configured model). DELIBERATELY
  // session-instance-only — never persisted — so a custom model can be
  // trialled for one search session without a config edit or server restart;
  // a reload restores the configured tier→model mapping.
  const [model, setModel] = useState("")
  // The ladder is a resilience setting, not part of a tier's preset, so a tier
  // change deliberately does NOT clear it the way it clears the override.
  const [fallbackModels, setFallbackModels] = useState<string[]>([])
  // Picking a tier selects the whole preset — budget AND model — so it CLEARS
  // any model override; the model pill then names the new tier's preset and
  // the user deviates from there if they want to. Without the reset, a stale
  // override from a previous tier would silently win over the fresh pick.
  const handleTierChange = useCallback((next: SearchTier) => {
    setTier(next)
    setModel("")
  }, [])

  // The run-config quartet, bundled into ONE prop threaded down to
  // ResultsPanel/LandingPanel → SearchBar → SearchScopeControl.
  const scope: SearchScope = useMemo(
    () => ({
      tier,
      onTierChange: handleTierChange,
      model,
      onModelChange: setModel,
      fallbackModels,
      onFallbackModelsChange: setFallbackModels,
    }),
    [tier, handleTierChange, model, fallbackModels]
  )

  // URL IS THE SINGLE SOURCE OF TRUTH for {workspace, active run}. Both
  // facets are DERIVED from the query string — see hooks/useSearchUrlState.ts
  // for the full contract (canonical shape, localStorage fallback, the
  // inert-landing invariant, and the push/replace transition table).
  const { runId, wsParam, workspace, selectWorkspaceParam, openRun, clearRun } =
    useSearchUrlState(workspaces, tier)

  // Live stream + durable snapshot, folded into one resolved run view — see
  // hooks/useResolvedRun.ts for the stream-vs-snapshot precedence and the
  // done/answerReady/elapsed derivation rules.
  const { stream, runQuery, run, done, answerReady, displayElapsed, resolvedWorkspaceId } =
    useResolvedRun(runId)

  // SHARABILITY CORE: reconcile `ws` from the run's resolved workspace once
  // it's known (see hooks/useSearchUrlState.ts's useReconcileWorkspaceParam
  // for the exactly-once-per-run-id contract).
  useReconcileWorkspaceParam({ runId, wsParam, resolvedWorkspaceId, selectWorkspaceParam })

  // Submit a run at an EXPLICIT tier (the composer's current tier by default).
  // "Go deeper" passes the next tier up; an explicit tier also overrides any
  // stale model override so a tier escalation picks the new tier's preset.
  const submitAtTier = (query: string, runTier: SearchTier, useModel: boolean) => {
    if (!workspace) return
    // Pre-submit guard: a workspace with no sources can't fan out — the
    // panels render the inline warning; never POST a doomed run.
    if (workspace.sources.length === 0) return
    if (startRunMutation.isPending) return
    // The resolved primary is already tried first, so it never belongs in its
    // own ladder.
    const primary = useModel ? model : ""
    const ladder = fallbackModels.filter((m) => m !== primary)
    startRunMutation.mutate(
      {
        workspace_id: workspace.id,
        query,
        tier: runTier,
        ...(useModel && model ? { model } : {}),
        ...(ladder.length > 0 ? { fallback_models: ladder } : {}),
      },
      {
        onSuccess: (res) => {
          // The run id arrives async from the POST — PUSH it (with the run's
          // workspace) into the URL so the result is shareable and Back returns
          // to the landing. This REPLACES any stale `run` param in place.
          openRun(res.run_id, workspace.id)
        },
        onError: (error) => {
          toast.error("Search failed", { description: error.message || "unknown error" })
        },
      }
    )
  }

  const handleSubmit = (query: string) => {
    submitAtTier(query, tier, true)
  }

  // "Go deeper": re-run the same query one tier up the ladder. Persist the new
  // tier (so the composer reflects it + a later plain submit stays at depth) and
  // clear the model override — escalating picks the new tier's preset.
  const handleDeeper = (query: string, nextTier: SearchTier) => {
    handleTierChange(nextTier)
    submitAtTier(query, nextTier, false)
  }

  // Cancel the in-flight run. Fire-and-forget — the live SSE stream's
  // `cancelled` terminal frame flips the view; this just requests it.
  const handleCancel = useCallback(() => {
    if (!runId) return
    cancelRunMutation.mutate(runId)
  }, [runId, cancelRunMutation])

  // Opening a stored run from any surface (replay chip, runs popover,
  // results-rail). Idempotent GET-only rehydration — never a POST.
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
      // A graph-lifecycle edit: changing the purpose/instructions/desc or
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
          <div className="text-sm font-medium text-[hsl(var(--destructive-text))]">
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
          onOpenChange={(o) => !o && setModal(null)}
          initial={null}
          sources={sources}
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
        <Suspense fallback={<RunChunkFallback />}>
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
            scope={scope}
            onRun={handleSubmit}
            onDeeper={handleDeeper}
            onCancel={handleCancel}
            onOpenRun={handleOpenRun}
            onOpenGraph={() => setGraphWorkspace(workspace)}
            onSelectWorkspace={handlePickWorkspace}
            onOpenCreate={() => setModal({ mode: "create" })}
            onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
          />
        </Suspense>
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
          scope={scope}
          submitting={submitting}
          onSelectWorkspace={handlePickWorkspace}
          onSubmit={handleSubmit}
          onOpenCreate={() => setModal({ mode: "create" })}
          onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
          onOpenSources={() => setSourcesOpen(true)}
          onOpenRun={handleOpenRun}
          onOpenGraph={setGraphWorkspace}
        />
      )}

      <WorkspaceModal
        open={modal !== null}
        onOpenChange={(o) => !o && setModal(null)}
        initial={editingWorkspace}
        sources={sources}
        onSubmit={handleSaveWorkspace}
        submitting={modalSubmitting}
      />

      <SourcesDialog
        open={sourcesOpen}
        sources={sources}
        onOpenChange={setSourcesOpen}
      />

      {graphWorkspace && (
        <Suspense fallback={null}>
          <WorkspaceGraphDialog
            open={graphWorkspace !== null}
            onOpenChange={(o) => !o && setGraphWorkspace(null)}
            workspace={graphWorkspace}
            onMapSource={() => {
              setGraphWorkspace(null)
              setSourcesOpen(true)
            }}
          />
        </Suspense>
      )}
    </>
  )
}

/** Shown only for the brief moment the lazily-imported ResultsPanel chunk is
 *  in flight (first run of a session). Mirrors the "Starting search…" loader so
 *  the transition reads as one continuous state, not a flash of new chrome. */
function RunChunkFallback() {
  return (
    <div className="flex-1 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-sm">
      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
      Loading results…
    </div>
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
    <div className="flex-1 overflow-y-auto" aria-busy="true">
      <section className="mx-auto max-w-[720px] w-full px-4 sm:px-6 flex flex-col items-center pt-[clamp(56px,12vh,140px)] pb-[clamp(32px,6vh,64px)]">
        {/* Placeholders track `ProductHero`'s real metrics — 56px mark (mb-5),
            48px title (mb-2.5), 15px/1.5 subtitle (mb-6) — so the hero lands in
            place instead of jumping when the queries settle. */}
        <SkeletonLine className="w-14 h-14 mb-5 rounded-full" />
        <SkeletonLine className="h-12 w-64 mb-2.5" />
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
    </div>
  )
}
