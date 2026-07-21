// Map-job SSE streaming reducer, split out of `useAgenticSearch.ts` (the
// TanStack query surface) — mirrors `runStream.ts`'s split for the run event
// log. Query keys (`mapJobsKey`/`SCG_KEY`/`SOURCES_KEY`) stay OWNED by the
// query file; this module imports them for its terminal-invalidation.

import { useEffect, useReducer } from "react"
import { useQueryClient } from "@tanstack/react-query"

import { streamMapJob } from "../api/agenticSearch"
import type { MapJobEvent, MapJobPhase } from "../types/agenticSearch"
import { mapJobsKey, SCG_KEY, SOURCES_KEY } from "./useAgenticSearch"

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
