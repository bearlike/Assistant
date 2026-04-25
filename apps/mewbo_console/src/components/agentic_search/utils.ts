import type { TraceAgent } from "../../types/agenticSearch"

export type AgentRunState = "queued" | "searching" | "done" | "empty"

export interface AgentSnapshot {
  state: AgentRunState
  visibleLines: TraceAgent["lines"]
  done: boolean
  running: boolean
}

/** Derive the agent's progress state at a given elapsed time. */
export function agentSnapshot(agent: TraceAgent, elapsed: number): AgentSnapshot {
  const visibleLines = agent.lines.filter((l) => l.t_ms <= elapsed)
  const done = agent.lines.some((l) => l.done && l.t_ms <= elapsed)
  const started = visibleLines.length > 0
  const last = agent.lines[agent.lines.length - 1]
  const empty = !!last?.empty
  const state: AgentRunState = !started
    ? "queued"
    : !done
    ? "searching"
    : empty
    ? "empty"
    : "done"
  return { state, visibleLines, done, running: started && !done }
}
