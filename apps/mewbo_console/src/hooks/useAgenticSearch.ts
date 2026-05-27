// TanStack Query hooks for the Agentic Search page + a streaming reducer over
// the run SSE event log. All server state flows through here so the view layer
// never touches fetch directly.

import { useEffect, useReducer, useRef } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  createWorkspace,
  deleteWorkspace,
  getRun,
  listSources,
  listWorkspaces,
  startRun,
  streamRun,
  updateWorkspace,
  type RunInput,
  type StartRunResult,
} from "../api/agenticSearch"
import type {
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
const WORKSPACES_KEY = ["agentic-search", "workspaces"] as const
const runKey = (runId: string | null) =>
  ["agentic-search", "run", runId] as const

export function useSources() {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: listSources,
    staleTime: Infinity, // catalog is static for the mock; cheap to keep
  })
}

export function useWorkspaces() {
  return useQuery({
    queryKey: WORKSPACES_KEY,
    queryFn: listWorkspaces,
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

/** Durable run snapshot — powers reload / deep-link rehydration. */
export function useRun(runId: string | null) {
  return useQuery({
    queryKey: runKey(runId),
    queryFn: () => getRun(runId as string),
    enabled: Boolean(runId),
    staleTime: 60_000,
  })
}

// ── Run streaming reducer ────────────────────────────────────────────────────

/** Folded UI state from the run SSE stream. Mirrors a `RunPayload` so the
 *  existing components can render directly off `toRunPayload(state)`. */
export interface RunStreamState {
  runId: string | null
  sessionId: string | null
  workspaceId: string | null
  query: string
  /** Wall-clock ms when `run_started` arrived — real elapsed basis. */
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

const initialRunStreamState: RunStreamState = {
  runId: null,
  sessionId: null,
  workspaceId: null,
  query: "",
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

function reduceRun(state: RunStreamState, event: SearchEvent): RunStreamState {
  switch (event.type) {
    case "run_started":
      return {
        ...initialRunStreamState,
        runId: event.run_id,
        sessionId: event.session_id,
        workspaceId: event.workspace_id,
        query: event.query,
        startedAt: Date.now(),
        status: "running",
      }
    case "agent_start": {
      if (state.trace.some((a) => a.agent_id === event.agent_id)) return state
      const agent: TraceAgent = {
        id: event.agent_id,
        agent_id: event.agent_id,
        name: event.name,
        source_id: event.source_id,
        slot: event.slot,
        lines: [],
      }
      return { ...state, trace: [...state.trace, agent] }
    }
    case "agent_line":
      return {
        ...state,
        trace: state.trace.map((a) =>
          a.agent_id === event.agent_id
            ? { ...a, lines: [...a.lines, event.line] }
            : a
        ),
      }
    case "agent_done":
      return {
        ...state,
        trace: state.trace.map((a) =>
          a.agent_id === event.agent_id
            ? {
                ...a,
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
    case "result":
      // Idempotent on idx-replay: skip a result we've already folded in.
      if (state.results.some((r) => r.id === event.result.id)) return state
      return { ...state, results: [...state.results, event.result] }
    case "answer_delta":
      // Streaming typewriter: appends until `answer_ready` replaces it.
      return state.answerReady
        ? state
        : { ...state, answer: { ...state.answer, tldr: state.answer.tldr + event.text } }
    case "answer_ready":
      return { ...state, answer: event.answer, answerReady: true }
    case "run_done":
      return { ...state, status: event.status, totalMs: event.total_ms, done: true }
    case "cancelled":
      return { ...state, status: "cancelled", done: true }
    case "error":
      return { ...state, status: "failed", error: event.error, done: true }
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
