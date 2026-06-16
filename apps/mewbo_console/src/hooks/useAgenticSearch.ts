// TanStack Query hooks for the Agentic Search page + a streaming reducer over
// the run SSE event log. All server state flows through here so the view layer
// never touches fetch directly.

import { useEffect, useReducer, useRef } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  cancelRun,
  createWorkspace,
  deleteWorkspace,
  fetchTiers,
  getRun,
  getScgStatus,
  getWorkspaceGraph,
  getWorkspaceGraphSummary,
  listMapJobs,
  listSources,
  listWorkspaceRuns,
  listWorkspaces,
  startMapJob,
  startRun,
  streamMapJob,
  streamRun,
  updateWorkspace,
  type RunInput,
  type StartRunResult,
} from "../api/agenticSearch"
import type {
  MapJobEvent,
  MapJobPhase,
  MapJobRecord,
  PastQuery,
  RunAnswer,
  RunPayload,
  RunStatus,
  SearchEvent,
  TraceAgent,
  Workspace,
  WorkspaceInput,
} from "../types/agenticSearch"

const SOURCES_KEY = ["agentic-search", "sources"] as const
const TIERS_KEY = ["agentic-search", "tiers"] as const
const WORKSPACES_KEY = ["agentic-search", "workspaces"] as const
const SCG_KEY = ["agentic-search", "scg"] as const
const runKey = (runId: string | null) =>
  ["agentic-search", "run", runId] as const
const mapJobsKey = (sourceId: string | null) =>
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

export function useDeleteWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteWorkspace(id),
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
 * The workspace-scoped SCG multiplex graph (#79). Pass `null` to keep the query
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
 * The workspace graph's `scope` + `stats` only (#139) — the cheap read the
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

/** Folded UI state from the map-job SSE stream (phase updates + terminal). */
export interface MapJobStreamState {
  phase: MapJobPhase | null
  done: boolean
  failed: boolean
  error: { code: string; message: string; hint?: string } | null
}

const initialMapJobStreamState: MapJobStreamState = {
  phase: null,
  done: false,
  failed: false,
  error: null,
}

function reduceMapJob(
  state: MapJobStreamState,
  event: MapJobEvent | { type: "reset" }
): MapJobStreamState {
  switch (event.type) {
    case "reset":
      return initialMapJobStreamState
    case "phase":
      return { ...state, phase: event.name }
    case "run_done":
      return { ...state, done: true, failed: event.status === "failed" }
    case "cancelled":
      return { ...state, done: true }
    case "error":
      return { ...state, done: true, failed: true, error: event.error }
    default:
      return state
  }
}

/**
 * Consume a map job's SSE event log into folded phase state. Mirrors
 * `useRunStream`: one `AbortController` per job, terminal event ends the fold.
 * On stream end the job-list, SCG, and source-catalog snapshots are invalidated
 * so the polling fallback, mapped-source badges, and SCG-driven tool ids catch
 * up immediately.
 */
export function useMapJobStream(sourceId: string | null, jobId: string | null) {
  const qc = useQueryClient()
  const [state, dispatch] = useReducer(reduceMapJob, initialMapJobStreamState)

  useEffect(() => {
    if (!sourceId || !jobId) return
    const ctrl = new AbortController()
    let cancelled = false
    dispatch({ type: "reset" }) // a new job must not inherit the old fold
    ;(async () => {
      try {
        for await (const event of streamMapJob(sourceId, { jobId, signal: ctrl.signal })) {
          if (cancelled) break
          dispatch(event)
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
          dispatch({
            type: "error",
            error: {
              code: "internal",
              message: err instanceof Error ? err.message : String(err),
            },
          })
        }
      } finally {
        if (!cancelled) {
          void qc.invalidateQueries({ queryKey: mapJobsKey(sourceId) })
          void qc.invalidateQueries({ queryKey: SCG_KEY })
          void qc.invalidateQueries({ queryKey: SOURCES_KEY })
        }
      }
    })()
    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [sourceId, jobId, qc])

  return state
}

// ── Run streaming reducer ────────────────────────────────────────────────────

/** Folded UI state from the run SSE stream. Mirrors a `RunPayload` so the
 *  existing components can render directly off `toRunPayload(state)`. */
export interface RunStreamState {
  runId: string | null
  sessionId: string | null
  workspaceId: string | null
  query: string
  /** True once the stream has folded ANY event — the authoritative "the live
   *  leg is attached, stop showing 'Starting search…'" signal. Decoupled from
   *  `runId` so a missed/buffered `run_started` opener can't wedge the view:
   *  the run view flips to the trace as soon as the first frame (e.g.
   *  `agent_start`) lands, not only on the server's `run_started` echo (#80). */
  attached: boolean
  /** Wall-clock ms when the first event arrived — real elapsed basis. */
  startedAt: number | null
  results: RunPayload["results"]
  /** Per-source agents keyed by agent_id, in arrival order. */
  trace: TraceAgent[]
  /** Streaming synthesis: `answer_delta` appends `tldr`; `answer_ready`
   *  replaces the whole block with the final cited answer. */
  answer: RunAnswer
  answerReady: boolean
  related_questions: string[]
  related_people: RunPayload["related_people"]
  status: RunStatus
  totalMs: number
  done: boolean
  error: { code: string; message: string; hint?: string } | null
}

const emptyAnswer: RunAnswer = {
  tldr: "",
  bullets: [],
  confidence: 0,
  sources_count: 0,
}

export const initialRunStreamState: RunStreamState = {
  runId: null,
  sessionId: null,
  workspaceId: null,
  query: "",
  attached: false,
  startedAt: null,
  results: [],
  trace: [],
  answer: emptyAnswer,
  answerReady: false,
  related_questions: [],
  related_people: [],
  status: "queued",
  totalMs: 0,
  done: false,
  error: null,
}

/** Synthetic action the hook dispatches the moment it begins streaming a run.
 *  Seeds the known `runId` (the stream arg) so the view attaches WITHOUT having
 *  to wait for the server's `run_started` echo, and resets the fold for a new
 *  run id. Carries no server payload — real fields fill in as events fold. */
interface RunStreamAttach {
  type: "attach"
  runId: string
}

/** Flip `attached` (and start the clock + status) the first time a real SSE
 *  frame folds — covers the case where the `run_started` opener was buffered /
 *  dropped and `agent_start` (or any frame) arrives first, so the view never
 *  wedges on "Starting search…" while events are demonstrably streaming. */
function ensureAttached(state: RunStreamState): RunStreamState {
  if (state.attached) return state
  return { ...state, attached: true, startedAt: Date.now(), status: "running" }
}

/** Exported for unit tests — components must render from this folded state. */
export function reduceRun(
  state: RunStreamState,
  event: SearchEvent | RunStreamAttach
): RunStreamState {
  switch (event.type) {
    case "attach":
      // New run id → fresh fold seeded with the known id. Same id (effect
      // re-run / replay) → keep the accumulated fold, don't wipe it.
      if (state.runId === event.runId && state.attached) return state
      return { ...initialRunStreamState, runId: event.runId }
    case "run_started":
      return {
        ...initialRunStreamState,
        runId: event.run_id,
        sessionId: event.session_id,
        workspaceId: event.workspace_id,
        query: event.query,
        attached: true,
        startedAt: Date.now(),
        status: "running",
      }
    case "agent_start": {
      state = ensureAttached(state)
      if (state.trace.some((a) => a.agent_id === event.agent_id)) return state
      const agent: TraceAgent = {
        id: event.agent_id,
        agent_id: event.agent_id,
        name: event.name,
        source_id: event.source_id,
        slot: event.slot,
        lines: [],
        // Instrument fidelity (additive): the lane's kind + driving model arrive
        // on `agent_start`, the rest (steps/duration/tokens/count) on done.
        kind: event.kind,
        model: event.model,
      }
      return { ...state, trace: [...state.trace, agent] }
    }
    case "agent_line": {
      state = ensureAttached(state)
      return {
        ...state,
        trace: state.trace.map((a) =>
          a.agent_id === event.agent_id
            ? { ...a, lines: [...a.lines, event.line] }
            : a
        ),
      }
    }
    case "agent_done": {
      state = ensureAttached(state)
      return {
        ...state,
        trace: state.trace.map((a) =>
          a.agent_id === event.agent_id
            ? {
                ...a,
                result: event.result ?? a.result,
                // Per-lane instrument totals fold onto the lane (additive — a
                // BE that doesn't emit them leaves the fields undefined).
                results_count: event.results_count,
                returned_count: event.returned_count ?? a.returned_count,
                steps: event.steps ?? a.steps,
                duration_ms: event.duration_ms ?? a.duration_ms,
                input_tokens: event.input_tokens ?? a.input_tokens,
                output_tokens: event.output_tokens ?? a.output_tokens,
                lines: [
                  ...a.lines,
                  {
                    glyph: event.empty ? "∅" : "✓",
                    text: event.empty
                      ? "no results"
                      : `${event.results_count} results`,
                    done: true,
                    empty: event.empty,
                  },
                ],
              }
            : a
        ),
      }
    }
    case "result": {
      state = ensureAttached(state)
      // Idempotent on idx-replay AND defensive against duplicate `result`
      // events (echo replay / snapshot+SSE merge): dedup strictly by id (#82).
      if (state.results.some((r) => r.id === event.result.id)) return state
      return { ...state, results: [...state.results, event.result] }
    }
    case "answer_delta":
      // Streaming typewriter: appends until `answer_ready` replaces it.
      return state.answerReady
        ? state
        : {
            ...ensureAttached(state),
            answer: { ...state.answer, tldr: state.answer.tldr + event.text },
          }
    case "answer_ready":
      return { ...ensureAttached(state), answer: event.answer, answerReady: true }
    case "related_questions":
      // Follow-ups from the parallel structured call — land them live (the
      // snapshot carries the same list on `RunPayload.related_questions`).
      return { ...ensureAttached(state), related_questions: event.questions }
    case "run_done":
      return { ...ensureAttached(state), status: event.status, totalMs: event.total_ms, done: true }
    case "cancelled":
      return { ...ensureAttached(state), status: "cancelled", done: true }
    case "error":
      return { ...ensureAttached(state), status: "failed", error: event.error, done: true }
    default:
      return state
  }
}

/**
 * Consume the run event stream into folded UI state. Owns an
 * `AbortController` per `runId` so unmounting / switching runs cancels the
 * in-flight stream. Stops folding once a terminal event lands.
 */
export function useRunStream(runId: string | null) {
  const [state, dispatch] = useReducer(reduceRun, initialRunStreamState)
  const controllerRef = useRef<AbortController | null>(null)

  useEffect(() => {
    if (!runId) return
    const ctrl = new AbortController()
    controllerRef.current = ctrl
    let cancelled = false
    // Seed the known run id into the fold immediately. `attached` still waits
    // for the first real frame, but this binds the stream state to THIS run id
    // so a stale fold from a previous run id is dropped on switch.
    dispatch({ type: "attach", runId })
    ;(async () => {
      try {
        for await (const event of streamRun(runId, { signal: ctrl.signal })) {
          if (cancelled) break
          dispatch(event)
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
          dispatch({
            type: "error",
            error: {
              code: "internal",
              message: err instanceof Error ? err.message : String(err),
            },
          })
        }
      }
    })()
    return () => {
      cancelled = true
      ctrl.abort()
    }
  }, [runId])

  return state
}

/**
 * Project folded stream state into a `RunPayload`-shaped object so existing
 * components render off a single, familiar shape. `total_ms` reflects real
 * elapsed (run start → now) while streaming, then the BE's final figure.
 */
export function toRunPayload(state: RunStreamState): RunPayload {
  const elapsed = state.startedAt != null ? Date.now() - state.startedAt : 0
  return {
    run_id: state.runId ?? "",
    session_id: state.sessionId ?? undefined,
    query: state.query,
    workspace_id: state.workspaceId ?? "",
    status: state.status,
    total_ms: state.done ? state.totalMs : elapsed,
    answer: state.answer,
    results: state.results,
    trace: state.trace,
    related_questions: state.related_questions,
    related_people: state.related_people,
    error: state.error?.message ?? null,
  }
}
