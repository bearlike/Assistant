import type { TraceAgent } from "../../types/agenticSearch"

export type AgentRunState = "queued" | "searching" | "done" | "empty"

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
