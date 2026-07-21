// Run SSE streaming reducer, split out of `useAgenticSearch.ts` (the TanStack
// query surface) — a self-contained fold over the run event log with no
// dependency on the query file (unlike `mapJobStream.ts`, which needs shared
// query keys for its terminal invalidation).

import { useEffect, useReducer, useRef } from "react"

import { streamRun } from "../api/agenticSearch"
import type {
  RunAnswer,
  RunPayload,
  RunStatus,
  SearchEvent,
  TraceAgent,
} from "../types/agenticSearch"

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
   *  `agent_start`) lands, not only on the server's `run_started` echo. */
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
      // events (echo replay / snapshot+SSE merge): dedup strictly by id.
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
