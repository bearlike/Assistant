import { DiffFile, EventRecord, TimelineEntry, TurnMeta } from "../types";
import { extractUnifiedDiffs, mergeDiffFiles } from "./diff";
import { parseStructuredResult } from "./logs";
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
        turnId: currentTurnId
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
