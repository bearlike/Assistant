import { DiffFile, EventRecord, TimelineEntry, TurnMeta, TurnTokenUsage } from "../types";
import { extractUnifiedDiffs, mergeDiffFiles } from "./diff";
import { parseStructuredResult } from "./logs";

function computeTurnTokenUsage(turnEvents: EventRecord[]): TurnTokenUsage | undefined {
  // PEAK for input (context pressure), SUM for output (additive).
  // Within a turn, input_tokens on each call grows as tool results stack
  // onto the same prompt — summing double-counts everything. The peak is
  // the real pressure on the model's context window.
  let peakRootInput = 0;
  let outputTokens = 0;
  let billedRootInput = 0;
  let billedSubInput = 0;
  const subPeakPerAgent = new Map<string, number>();
  let subOutputTokens = 0;
  const subAgents = new Set<string>();
  let cacheCreationTokens = 0;
  let cacheReadTokens = 0;
  let reasoningTokens = 0;
  for (const e of turnEvents) {
    if (e.type !== "llm_call_end") continue;
    const p = e.payload as Record<string, unknown>;
    const depth = typeof p.depth === "number" ? p.depth : 0;
    const inTok = typeof p.input_tokens === "number" ? p.input_tokens : 0;
    const outTok = typeof p.output_tokens === "number" ? p.output_tokens : 0;
    const cacheCreate =
      typeof p.cache_creation_input_tokens === "number"
        ? p.cache_creation_input_tokens
        : 0;
    const cacheRead =
      typeof p.cache_read_input_tokens === "number"
        ? p.cache_read_input_tokens
        : 0;
    const reasoning =
      typeof p.reasoning_output_tokens === "number"
        ? p.reasoning_output_tokens
        : 0;
    cacheCreationTokens += cacheCreate;
    cacheReadTokens += cacheRead;
    reasoningTokens += reasoning;
    if (depth === 0) {
      if (inTok > peakRootInput) peakRootInput = inTok;
      outputTokens += outTok;
      billedRootInput += inTok;
    } else {
      const aid = typeof p.agent_id === "string" ? p.agent_id : "";
      const prev = subPeakPerAgent.get(aid) ?? 0;
      if (inTok > prev) subPeakPerAgent.set(aid, inTok);
      subOutputTokens += outTok;
      billedSubInput += inTok;
      if (aid) subAgents.add(aid);
    }
  }
  // Each sub-agent runs in its own isolated context, so summing per-agent
  // peaks tells us "combined peak pressure across parallel sub-contexts".
  let subInputTokens = 0;
  for (const v of subPeakPerAgent.values()) subInputTokens += v;
  if (!peakRootInput && !outputTokens && !subInputTokens && !subOutputTokens) {
    return undefined;
  }
  return {
    inputTokens: peakRootInput,
    outputTokens,
    subInputTokens,
    subOutputTokens,
    subAgentCount: subAgents.size,
    cacheCreationTokens,
    cacheReadTokens,
    reasoningTokens,
    billedInputTokens: billedRootInput + billedSubInput,
  };
}
export function buildTimeline(events: EventRecord[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let turnStart: string | undefined;
  let diffFiles: DiffFile[] = [];
  let lastModel: string | undefined;
  let turnModel: string | undefined;
  for (const event of events) {
    if (event.type === "context") {
      const payload = event.payload as { model?: string } | undefined;
      if (payload?.model) lastModel = payload.model;
      continue;
    }
    if (event.type === "user") {
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      turnStart = event.ts;
      turnModel = lastModel;
      entries.push({
        id: `user-${turnIndex}`,
        role: "user",
        content: String(event.payload?.text ?? ""),
        turnId: currentTurnId,
        ts: event.ts,
      });
      continue;
    }
    if (!currentTurnId) {
      continue;
    }
    turnEvents.push(event);
    if (event.type === "tool_result") {
      const result = event.payload?.result;
      if (typeof result === "string") {
        const parsed = parseStructuredResult(result);
        const diffSource = parsed.kind === "diff" ? parsed.text : parsed.kind === "raw" ? parsed.text : "";
        if (diffSource) {
          diffFiles.push(...extractUnifiedDiffs(diffSource));
        }
      }
    }
    if (event.type === "plan_proposed") {
      const payload = event.payload || {};
      const revision = typeof payload.revision === "number" ? payload.revision : 0;
      entries.push({
        id: `plan-${revision}`,
        role: "plan",
        content: "",
        turnId: currentTurnId,
        plan: {
          revision,
          status: "pending",
          planPath: typeof payload.plan_path === "string" ? payload.plan_path : undefined,
          planContent: typeof payload.content === "string" ? payload.content : "",
          planSummary: typeof payload.summary === "string" ? payload.summary : undefined,
          timestamp: event.ts
        }
      });
      continue;
    }
    if (event.type === "plan_approved" || event.type === "plan_rejected") {
      const payload = event.payload || {};
      const revision = typeof payload.revision === "number" ? payload.revision : 0;
      const nextStatus = event.type === "plan_approved" ? "approved" : "rejected";
      for (let i = entries.length - 1; i >= 0; i -= 1) {
        const entry = entries[i];
        if (entry.role === "plan" && entry.plan?.revision === revision) {
          entry.plan = { ...entry.plan, status: nextStatus };
          break;
        }
      }
      continue;
    }
    if (event.type === "assistant") {
      const duration = formatDuration(turnStart, event.ts);
      const turn: TurnMeta = {
        id: currentTurnId,
        events: turnEvents,
        duration,
        files: mergeDiffFiles(diffFiles),
        model: turnModel,
        tokenUsage: computeTurnTokenUsage(turnEvents),
      };
      entries.push({
        id: `assistant-${turnIndex}`,
        role: "assistant",
        content: String(event.payload?.text ?? ""),
        turnId: currentTurnId,
        turn
      });
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
      turnStart = undefined;
    }
    // Defensive fallback: materialise the turn on completion when no
    // prior assistant event has closed it. Handles legacy sessions (and
    // any race where a run terminates without writing a final assistant
    // event) — without this the failed turn's tool_results/agent_messages
    // stay orphaned in turnEvents and are silently discarded when the
    // next user turn resets state.
    if (event.type === "completion" && currentTurnId) {
      const duration = formatDuration(turnStart, event.ts);
      const reason = String(
        (event.payload as { done_reason?: string } | undefined)?.done_reason ?? ""
      );
      const content =
        reason === "error" ? "(run interrupted — see logs)" :
        reason === "max_steps_reached" ? "(step limit reached)" :
        reason === "canceled" || reason === "cancelled" ? "(run canceled)" :
        "(run ended)";
      const turn: TurnMeta = {
        id: currentTurnId,
        events: turnEvents,
        duration,
        files: mergeDiffFiles(diffFiles),
        model: turnModel,
        tokenUsage: computeTurnTokenUsage(turnEvents),
      };
      entries.push({
        id: `completion-${turnIndex}`,
        role: "assistant",
        content,
        turnId: currentTurnId,
        turn
      });
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
      turnStart = undefined;
    }
  }
  return entries;
}

export function getActiveTurn(events: EventRecord[]): TurnMeta | null {
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let diffFiles: DiffFile[] = [];
  let lastModel: string | undefined;
  let turnModel: string | undefined;
  for (const event of events) {
    if (event.type === "context") {
      const payload = event.payload as { model?: string } | undefined;
      if (payload?.model) lastModel = payload.model;
      continue;
    }
    if (event.type === "user") {
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      turnModel = lastModel;
      continue;
    }
    if (!currentTurnId) {
      continue;
    }
    turnEvents.push(event);
    if (event.type === "tool_result") {
      const result = event.payload?.result;
      if (typeof result === "string") {
        const parsed = parseStructuredResult(result);
        const diffSource = parsed.kind === "diff" ? parsed.text : parsed.kind === "raw" ? parsed.text : "";
        if (diffSource) {
          diffFiles.push(...extractUnifiedDiffs(diffSource));
        }
      }
    }
    if (event.type === "assistant") {
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
    }
    // Match buildTimeline: a completion event also closes the active turn.
    if (event.type === "completion" && currentTurnId) {
      currentTurnId = null;
      turnEvents = [];
      diffFiles = [];
    }
  }
  if (!currentTurnId) {
    return null;
  }
  return {
    id: currentTurnId,
    events: turnEvents,
    files: mergeDiffFiles(diffFiles),
    model: turnModel,
  };
}
function formatDuration(start?: string, end?: string): string | undefined {
  if (!start || !end) {
    return undefined;
  }
  const startMs = Date.parse(start);
  const endMs = Date.parse(end);
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || endMs <= startMs) {
    return undefined;
  }
  const totalSeconds = Math.floor((endMs - startMs) / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes > 0) {
    return `${minutes}m ${seconds}s`;
  }
  return `${seconds}s`;
}
