// TanStack Query hooks for the Agentic Search page. All server state flows
// through here so the view layer never touches fetch directly. The two
// SSE-streaming reducers live alongside this as siblings — `runStream.ts`
// (self-contained) and `mapJobStream.ts` (imports this file's query keys for
// its terminal invalidation) — not re-exported here; consumers import them
// directly.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  cancelRun,
  createWorkspace,
  fetchTiers,
  getRun,
  getScgStatus,
  getWorkspaceGraph,
  getWorkspaceGraphSummary,
  listMapJobs,
  listRecentSearchRuns,
  listSources,
  listWorkspaceRuns,
  listWorkspaces,
  startMapJob,
  startRun,
  updateWorkspace,
  type RecentSearchRun,
  type RunInput,
  type StartRunResult,
} from "../api/agenticSearch"
import type {
  MapJobRecord,
  PastQuery,
  Workspace,
  WorkspaceInput,
} from "../types/agenticSearch"

export const SOURCES_KEY = ["agentic-search", "sources"] as const
const RECENT_RUNS_KEY = ["agentic-search", "recent-runs"] as const
const TIERS_KEY = ["agentic-search", "tiers"] as const
const WORKSPACES_KEY = ["agentic-search", "workspaces"] as const
export const SCG_KEY = ["agentic-search", "scg"] as const
const runKey = (runId: string | null) =>
  ["agentic-search", "run", runId] as const
export const mapJobsKey = (sourceId: string | null) =>
  ["agentic-search", "map-jobs", sourceId] as const
const workspaceRunsKey = (workspaceId: string | null) =>
  ["agentic-search", "workspace-runs", workspaceId] as const
const workspaceGraphKey = (workspaceId: string | null) =>
  ["agentic-search", "workspace-graph", workspaceId] as const
const workspaceGraphSummaryKey = (workspaceId: string | null) =>
  ["agentic-search", "workspace-graph-summary", workspaceId] as const

export function useSources() {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: listSources,
    // The catalog is live (configured servers + SCG tool overrides); map-job
    // completion invalidates SOURCES_KEY so freshly-mapped tools show up.
    staleTime: 60_000,
  })
}

/**
 * Recent search runs across every workspace, for the NavRail's Search section.
 * The backend route is a parallel deliverable; until it lands the request 404s,
 * so the query fn swallows any failure into an empty list — the rail shows its
 * calm empty state, never error residue (`retry: false` keeps it from hammering
 * the missing route).
 */
export function useRecentSearchRuns(limit = 30) {
  return useQuery<RecentSearchRun[]>({
    queryKey: RECENT_RUNS_KEY,
    queryFn: async () => {
      try {
        return await listRecentSearchRuns(limit)
      } catch {
        return []
      }
    },
    staleTime: 30_000,
    retry: false,
  })
}

/** Tier→model presets (`GET /tiers`) — config-backed, changes only on a
 *  settings edit, so a long staleTime keeps the composer render cheap. */
export function useTiers() {
  return useQuery({
    queryKey: TIERS_KEY,
    queryFn: fetchTiers,
    staleTime: 5 * 60_000,
  })
}

export function useWorkspaces() {
  return useQuery({
    queryKey: WORKSPACES_KEY,
    queryFn: () => listWorkspaces(),
    staleTime: 60_000,
  })
}

export function useCreateWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: WorkspaceInput) => createWorkspace(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

export function useUpdateWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: Partial<WorkspaceInput> }) =>
      updateWorkspace(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

/**
 * Optimistically prepend a freshly-run query onto the active workspace's
 * `past_queries` in the cache. Avoids a full `WORKSPACES_KEY` refetch (which
 * would re-pull EVERY workspace just to bump one history list).
 */
function bumpPastQueries(
  qc: ReturnType<typeof useQueryClient>,
  workspaceId: string,
  entry: PastQuery
): void {
  qc.setQueryData<Workspace[]>(WORKSPACES_KEY, (prev) => {
    if (!prev) return prev
    return prev.map((w) =>
      w.id === workspaceId
        ? { ...w, past_queries: [entry, ...w.past_queries] }
        : w
    )
  })
}

/** Start a run, returning the synchronous `{run, run_id, session_id}` envelope. */
export function useStartRun() {
  const qc = useQueryClient()
  return useMutation<StartRunResult, Error, RunInput>({
    mutationFn: (input) => startRun(input),
    // The live stream (useRunStream) and the durable snapshot (useRun) own the
    // run cache; don't seed it with a differently-shaped RunPayload here. Just
    // optimistically bump the active workspace's past-queries history.
    onSuccess: (res, input) => {
      bumpPastQueries(qc, input.workspace_id, {
        q: input.query,
        when: "just now",
        results: res.run.results.length,
        ran_at: new Date().toISOString(),
        run_id: res.run_id,
        status: res.status ?? res.run.status ?? "completed",
      })
    },
  })
}

/**
 * Cancel an in-flight run (`POST /runs/<id>/cancel`). Fire-and-forget: the live
 * SSE stream emits the `cancelled` terminal frame that flips the view, so this
 * mutation seeds no cache — it's pure steering, like the composer's Stop.
 */
export function useCancelRun() {
  return useMutation<void, Error, string>({
    mutationFn: (runId) => cancelRun(runId),
  })
}

/**
 * A workspace's persisted run history. Pass `null` to keep the query idle —
 * callers (e.g. the workspace-card popover) enable it lazily on open.
 */
export function useWorkspaceRuns(workspaceId: string | null) {
  return useQuery({
    queryKey: workspaceRunsKey(workspaceId),
    queryFn: () => listWorkspaceRuns(workspaceId as string),
    enabled: Boolean(workspaceId),
    staleTime: 30_000,
  })
}

/**
 * The workspace-scoped SCG multiplex graph. Pass `null` to keep the query
 * idle — the graph dialog enables it lazily on open. Invalidated alongside the
 * SCG/map-job queries so a freshly-mapped source shows up in an open graph.
 */
export function useWorkspaceGraph(workspaceId: string | null) {
  return useQuery({
    queryKey: workspaceGraphKey(workspaceId),
    queryFn: () => getWorkspaceGraph(workspaceId as string),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
  })
}

/**
 * The workspace graph's `scope` + `stats` only — the cheap read the
 * landing health band uses. Pass `null` to keep it idle. Separate query key
 * from `useWorkspaceGraph` so the band never pulls the full node/edge payload
 * onto the landing critical path; both share the BE's warm `query_nodes` cache.
 */
export function useWorkspaceGraphSummary(workspaceId: string | null) {
  return useQuery({
    queryKey: workspaceGraphSummaryKey(workspaceId),
    queryFn: () => getWorkspaceGraphSummary(workspaceId as string),
    enabled: Boolean(workspaceId),
    staleTime: 60_000,
  })
}

/** Durable run snapshot — powers reload / deep-link rehydration. */
export function useRun(runId: string | null) {
  return useQuery({
    queryKey: runKey(runId),
    queryFn: () => getRun(runId as string),
    enabled: Boolean(runId),
    staleTime: 60_000,
  })
}

// ── SCG introspection + map-source jobs ─────────────────────────────────────

/** SCG introspection (`GET /scg`). `enabled: false` when the feature is off. */
export function useScgStatus(enabled = true) {
  return useQuery({
    queryKey: SCG_KEY,
    queryFn: getScgStatus,
    enabled,
    staleTime: 60_000,
  })
}

/** A map job that hasn't reached a terminal status yet. */
export function isMapJobActive(job: MapJobRecord | undefined): boolean {
  return job != null && (job.status === "queued" || job.status === "running")
}

/**
 * Latest-first map jobs for a source. Polls while the newest job is still
 * active — the reload-safe fallback when the SSE stream isn't (yet) attached.
 */
export function useMapJobs(sourceId: string | null) {
  return useQuery({
    queryKey: mapJobsKey(sourceId),
    queryFn: () => listMapJobs(sourceId as string),
    enabled: Boolean(sourceId),
    refetchInterval: (query) =>
      isMapJobActive(query.state.data?.[0]) ? 2_000 : false,
  })
}

/** Start a map-source (SCG indexing) job for one connector. */
export function useStartMapJob() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ sourceId, sourceType }: { sourceId: string; sourceType: string }) =>
      startMapJob(sourceId, { source_type: sourceType }),
    onSuccess: (_res, { sourceId }) =>
      qc.invalidateQueries({ queryKey: mapJobsKey(sourceId) }),
  })
}
