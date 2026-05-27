import { useEffect, useMemo, useState } from "react"
import { Loader2 } from "lucide-react"

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
import type { RunPayload, Workspace, WorkspaceInput } from "../../types/agenticSearch"
import { LandingPanel } from "./LandingPanel"
import { ResultsPanel } from "./ResultsPanel"
import { WorkspaceModal } from "./WorkspaceModal"

const STORAGE_WORKSPACE = "agentic-search:workspace-id"
const STORAGE_RUN = "agentic-search:run-id"

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

  const [workspaceId, setWorkspaceId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null
    return window.localStorage.getItem(STORAGE_WORKSPACE)
  })
  const [modal, setModal] = useState<ModalState>(null)
  // The active run id drives the live stream + snapshot rehydration. Persisted
  // so a reload re-opens the last run via getRun / the replayed SSE stream.
  const [runId, setRunId] = useState<string | null>(() => {
    if (typeof window === "undefined") return null
    return window.localStorage.getItem(STORAGE_RUN)
  })

  // Resolve the current workspace, falling back to the first available one
  // if the persisted id is gone or no id has been chosen yet.
  const workspace = useMemo<Workspace | null>(() => {
    if (workspaces.length === 0) return null
    return workspaces.find((w) => w.id === workspaceId) ?? workspaces[0]
  }, [workspaces, workspaceId])

  // Persist the active workspace and reconcile when it changes.
  useEffect(() => {
    if (!workspace) return
    if (workspaceId !== workspace.id) {
      setWorkspaceId(workspace.id)
    }
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_WORKSPACE, workspace.id)
    }
  }, [workspace, workspaceId])

  useEffect(() => {
    if (typeof window === "undefined") return
    if (runId) window.localStorage.setItem(STORAGE_RUN, runId)
    else window.localStorage.removeItem(STORAGE_RUN)
  }, [runId])

  // Live stream (folds the run's SSE event log) + durable snapshot fallback
  // for reload rehydration before the stream has replayed run_started.
  const stream = useRunStream(runId)
  const runQuery = useRun(runId)

  // Real elapsed: ticks while the run is live, freezes on terminal.
  const elapsedMs = useElapsedMs(stream.startedAt, !stream.done)

  // Prefer live stream state once it has begun; otherwise rehydrate from the
  // snapshot so a reload shows the finished run immediately.
  const run: RunPayload | null = useMemo(() => {
    if (stream.runId) return toRunPayload(stream)
    return runQuery.data?.payload ?? null
  }, [stream, runQuery.data])

  // When rendering from a snapshot (no live stream yet) the run is terminal.
  const streaming = Boolean(stream.runId)
  const done = streaming ? stream.done : true
  const answerReady = streaming ? stream.answerReady : true
  const snapshotElapsed = run?.total_ms ?? 0
  const displayElapsed = streaming ? elapsedMs : snapshotElapsed

  const handleSubmit = (query: string) => {
    if (!workspace) return
    startRunMutation.mutate(
      { workspace_id: workspace.id, query },
      { onSuccess: (res) => setRunId(res.run_id) }
    )
  }

  // ROUND-TRIP FIX #2: switching workspaces changes selection ONLY. It must
  // not auto-re-run the last query — the user submits explicitly.
  const handlePickWorkspace = (next: Workspace) => {
    setWorkspaceId(next.id)
  }

  const handleSaveWorkspace = (values: WorkspaceInput) => {
    if (modal?.mode === "edit") {
      updateWorkspaceMutation.mutate(
        { id: modal.workspaceId, input: values },
        {
          onSuccess: (updated) => {
            setWorkspaceId(updated.id)
            setModal(null)
          },
        }
      )
    } else if (modal?.mode === "create") {
      createWorkspaceMutation.mutate(values, {
        onSuccess: (created) => {
          setWorkspaceId(created.id)
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

  if (sourcesQuery.isLoading || workspacesQuery.isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center text-[hsl(var(--muted-foreground))] text-sm">
        <Loader2 className="h-4 w-4 mr-2 animate-spin" />
        Loading workspaces…
      </div>
    )
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
    return (
      <div className="flex-1 flex items-center justify-center p-6 text-center">
        <div>
          <div className="text-sm font-medium">No workspaces yet.</div>
          <button
            type="button"
            onClick={() => setModal({ mode: "create" })}
            className="mt-3 inline-flex items-center gap-1.5 px-3 h-9 rounded-full bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] text-sm hover:opacity-90"
          >
            Create your first workspace
          </button>
        </div>
      </div>
    )
  }

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
          isLoading={startRunMutation.isPending || (Boolean(runId) && runQuery.isLoading && !stream.runId)}
          onRun={handleSubmit}
          onPickWorkspace={handlePickWorkspace}
          onOpenCreate={() => setModal({ mode: "create" })}
          onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
        />
      ) : (
        <LandingPanel
          workspace={workspace}
          workspaces={workspaces}
          sources={sources}
          onPickWorkspace={handlePickWorkspace}
          onSubmit={handleSubmit}
          onOpenCreate={() => setModal({ mode: "create" })}
          onOpenConfig={(w) => setModal({ mode: "edit", workspaceId: w.id })}
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
    </>
  )
}
