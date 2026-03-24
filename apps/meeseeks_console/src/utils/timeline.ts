import { DiffFile, EventRecord, TimelineEntry, TurnMeta } from "../types";
import { extractUnifiedDiffs, mergeDiffFiles } from "./diff";
export function buildTimeline(events: EventRecord[]): TimelineEntry[] {
  const entries: TimelineEntry[] = [];
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let turnStart: string | undefined;
  let diffFiles: DiffFile[] = [];
  for (const event of events) {
    if (event.type === "user") {
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      turnStart = event.ts;
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
        diffFiles.push(...extractUnifiedDiffs(result));
      }
    }
    if (event.type === "assistant") {
      const duration = formatDuration(turnStart, event.ts);
      const turn: TurnMeta = {
        id: currentTurnId,
        events: turnEvents,
        duration,
        files: mergeDiffFiles(diffFiles)
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
  }
  return entries;
}

export function getActiveTurn(events: EventRecord[]): TurnMeta | null {
  let turnIndex = 0;
  let currentTurnId: string | null = null;
  let turnEvents: EventRecord[] = [];
  let diffFiles: DiffFile[] = [];
  for (const event of events) {
    if (event.type === "user") {
      turnIndex += 1;
      currentTurnId = `turn-${turnIndex}`;
      turnEvents = [event];
      diffFiles = [];
      continue;
    }
    if (!currentTurnId) {
      continue;
    }
    turnEvents.push(event);
    if (event.type === "tool_result") {
      const result = event.payload?.result;
      if (typeof result === "string") {
        diffFiles.push(...extractUnifiedDiffs(result));
      }
    }
    if (event.type === "assistant") {
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
    files: mergeDiffFiles(diffFiles)
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
