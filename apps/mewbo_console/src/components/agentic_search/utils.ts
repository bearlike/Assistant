import type { PastQuery, SourceCatalogEntry, TraceAgent } from "../../types/agenticSearch"

export type AgentRunState = "queued" | "searching" | "done" | "empty"

/**
 * Stable, collision-free identity for a past-query entry. cmdk identifies
 * `CommandItem`s by their `value`, and React lists by `key` — so two history
 * rows sharing the same identity hover/select as one (the "run a query 3×, all
 * three highlight together" bug). The run_id is unique per stored run; when an
 * entry predates run_id we fall back to `<q>-<index>` (the list index keeps it
 * unique even for identical text). Use this for BOTH the cmdk `value` and the
 * React `key` wherever past queries render interactively.
 */
export function pastQueryKey(p: PastQuery, index: number): string {
  return p.run_id ?? `${p.q}-${index}`
}

/**
 * Dedupe a past-query list by normalized (trimmed, casefolded) query text,
 * keeping the FIRST occurrence. The backend prepends new entries, so first ==
 * most recent — re-running an identical query collapses to its latest run.
 * Duplicates are dropped entirely so they're never selectable. Order of the
 * surviving entries is preserved (recency-first).
 */
export function dedupePastQueries(queries: PastQuery[]): PastQuery[] {
  const seen = new Set<string>()
  const out: PastQuery[] = []
  for (const p of queries) {
    const norm = p.q.trim().toLowerCase()
    if (seen.has(norm)) continue
    seen.add(norm)
    out.push(p)
  }
  return out
}

/**
 * Resolve a trace lane to its catalog source. The orchestrated runner emits one
 * extra "coordinator" lane (the root agent's tool activity, named `scg-search`)
 * whose `source_id` is "" — it doesn't map to any catalog connector. Such a lane
 * is NOT a per-source probe: it has no SrcAvatar and no per-source result count.
 * `isCoordinator` is true whenever the lane can't be resolved to a catalog
 * source, so callers render it gracefully (coordinator glyph, no misleading 0).
 */
export interface LaneSource {
  source: SourceCatalogEntry | undefined
  isCoordinator: boolean
}

export function laneSource(
  agent: TraceAgent,
  sources: SourceCatalogEntry[]
): LaneSource {
  const source = agent.source_id
    ? sources.find((s) => s.id === agent.source_id)
    : undefined
  return { source, isCoordinator: !source }
}

export interface AgentSnapshot {
  state: AgentRunState
  visibleLines: TraceAgent["lines"]
  done: boolean
  running: boolean
}

/**
 * Derive an agent's progress state from REAL stream state. Every line in
 * `agent.lines` has already arrived over SSE, so visibility is the full set —
 * we no longer gate on a fake `elapsed` timer. `done` / `empty` come from the
 * terminal line the BE emits on `agent_done`.
 */
export function agentSnapshot(agent: TraceAgent): AgentSnapshot {
  const visibleLines = agent.lines
  const done = agent.lines.some((l) => l.done)
  const started = visibleLines.length > 0
  const empty = agent.lines.some((l) => l.empty)
  const state: AgentRunState = !started
    ? "queued"
    : !done
    ? "searching"
    : empty
    ? "empty"
    : "done"
  return { state, visibleLines, done, running: started && !done }
}

/**
 * Fraction (0..1) of spawned agents that have finished, for the run progress
 * bar. Single source of truth: ProgressStrip, RightRail, and TraceDrawer all
 * call this so the bar never diverges. Once the run is terminal the bar is
 * full even if an agent ended empty.
 */
export function runProgress(agents: TraceAgent[], done: boolean): number {
  if (done) return 1
  if (agents.length === 0) return 0
  return agents.filter((a) => agentSnapshot(a).done).length / agents.length
}

/**
 * Humanize a millisecond duration for the trace instrument rows: sub-second →
 * "420ms", under a minute → "4.1s", longer → "1m 12s". Returns "" for
 * null/undefined/non-finite so the field stays silent (honesty rule — the
 * trace panel renders only present values, never a fabricated 0).
 */
export function humanizeMs(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return ""
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(1)}s`
  const m = Math.floor(s / 60)
  const rem = Math.round(s - m * 60)
  return `${m}m ${rem}s`
}

/** Compact a token count: 850 → "850", 12_400 → "12.4k". "" for absent. */
export function compactTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return ""
  if (n < 1000) return String(n)
  return `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k`
}
