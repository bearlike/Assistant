import { useMemo } from "react"

import { toRunPayload, useRunStream } from "./runStream"
import { useRun } from "./useAgenticSearch"
import { useElapsedMs } from "./useElapsed"
import type { RunPayload, RunRecord } from "../types/agenticSearch"

/**
 * Resolve a snapshot-loaded run's elapsed ms. Prefers the BE's stamped
 * `total_ms`; when that's 0/absent, falls back to the run record's own ISO
 * timestamps (`created_at` → `completed_at`) — fields the wire already carries.
 * Returns 0 only when neither is usable, so the status line shows "duration
 * unknown" (silence) rather than a fabricated zero. Never invents a value.
 */
function snapshotDuration(totalMs: number, record: RunRecord | undefined): number {
  if (totalMs > 0) return totalMs
  if (!record?.created_at || !record.completed_at) return 0
  const start = Date.parse(record.created_at)
  const end = Date.parse(record.completed_at)
  if (!Number.isFinite(start) || !Number.isFinite(end)) return 0
  return Math.max(0, end - start)
}

export interface ResolvedRun {
  stream: ReturnType<typeof useRunStream>
  runQuery: ReturnType<typeof useRun>
  run: RunPayload | null
  done: boolean
  answerReady: boolean
  displayElapsed: number
  /** The run's own workspace id, off whichever leg (stream/snapshot) is
   *  authoritative right now — feeds `useReconcileWorkspaceParam`. */
  resolvedWorkspaceId: string | null
}

/**
 * Live stream (folds the run's SSE event log) + durable snapshot fallback
 * for reload rehydration before the stream has replayed run_started, folded
 * into one resolved view of the run.
 *
 * Prefer live stream state once the leg has ATTACHED (≥1 real SSE frame
 * folded); otherwise rehydrate from the snapshot so a reload shows the
 * finished run immediately. `stream.attached` — not `stream.runId` — is the
 * gate: the hook seeds `runId` synchronously on subscribe, so keying off it
 * would render an empty payload before any event arrives. Attaching on the
 * FIRST frame (incl. `agent_start`) is what flips the live view off
 * "Starting search…" even if the `run_started` opener was dropped.
 *
 * Done-ness pairs with the AUTHORITATIVE run status, not "rendered from a
 * snapshot ⇒ terminal". A live stream owns it once attached; before
 * that, the snapshot's own `status` decides — a still-`running` snapshot
 * (an async orchestrated run rehydrated via deep-link before its SSE
 * attaches) must NOT render terminally. Only a genuinely terminal status
 * (completed/failed/cancelled) is `done`.
 */
export function useResolvedRun(runId: string | null): ResolvedRun {
  const stream = useRunStream(runId)
  const runQuery = useRun(runId)

  // Real elapsed: ticks while the run is live, freezes on terminal.
  const elapsedMs = useElapsedMs(stream.startedAt, !stream.done)

  const run: RunPayload | null = useMemo(() => {
    if (stream.attached) return toRunPayload(stream)
    return runQuery.data?.payload ?? null
  }, [stream, runQuery.data])

  const streaming = stream.attached
  const snapshotStatus = runQuery.data?.status
  const snapshotTerminal =
    snapshotStatus === "completed" ||
    snapshotStatus === "failed" ||
    snapshotStatus === "cancelled"
  const done = streaming ? stream.done : snapshotTerminal
  const answerReady = streaming ? stream.answerReady : snapshotTerminal

  // Snapshot-loaded runs: prefer the BE's real `total_ms`; when it's 0/absent
  // (older records, or before the BE stamped it) DERIVE the duration from the
  // run record's own ISO timestamps (`created_at` → `completed_at`), which the
  // wire already carries on `RunRecord`. No invented values — a run with no
  // usable timestamps yields 0, which the band renders as "duration unknown"
  // (silence), never a fabricated "0.0s".
  const snapshotElapsed = useMemo(
    () => snapshotDuration(run?.total_ms ?? 0, runQuery.data),
    [run?.total_ms, runQuery.data]
  )
  const displayElapsed = streaming ? elapsedMs : snapshotElapsed

  const resolvedWorkspaceId = stream.attached
    ? stream.workspaceId
    : runQuery.data?.workspace_id ?? null

  return { stream, runQuery, run, done, answerReady, displayElapsed, resolvedWorkspaceId }
}
