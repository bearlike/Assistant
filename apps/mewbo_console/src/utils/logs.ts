import { AgentTreeNode, EventRecord, LogEntry, PlanLogEntry, PlanStep, RunErrorDetail, RunErrorKind, RunFailureMeta, RunFailureReason } from "../types";
import { readBool, readNumber, readString } from "./payload";

// ── Shared completion-failure parser ─────────────────────────────────
// Both buildLogs() and buildTimeline() normalize a failed `completion`
// through this, so the conversation card and the trace card can never
// disagree about what failed.

const RUN_ERROR_KINDS: ReadonlySet<string> = new Set<RunErrorKind>([
  "upstream_bad_gateway",
  "rate_limited",
  "timeout",
  "auth",
  "context_overflow",
  "provider_unavailable",
  "tool_failure",
  "unknown",
]);

/** Narrow an untrusted `error_detail` payload, or undefined if it isn't one. */
function parseErrorDetail(raw: unknown): RunErrorDetail | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const o = raw as Record<string, unknown>;
  const detail = readString(o, "detail") ?? "";
  const kindRaw = readString(o, "kind") ?? "";
  return {
    kind: RUN_ERROR_KINDS.has(kindRaw) ? (kindRaw as RunErrorKind) : "unknown",
    title: readString(o, "title") ?? "",
    provider: readString(o, "provider"),
    detail,
    failure_reason: readString(o, "failure_reason"),
    models_tried: Array.isArray(o.models_tried)
      ? o.models_tried.map(String)
      : undefined,
    // Trust the reported original length only if it's a sane number; the
    // "showing N of M" footer is a lie otherwise.
    detail_chars:
      typeof o.detail_chars === "number" && o.detail_chars >= detail.length
        ? o.detail_chars
        : detail.length,
    truncated: o.truncated === true,
  };
}

// A completion whose `done_reason` is any of these is a failure the user
// should see and recover from. `unmet_goal`/`verification_failed`/
// `halted_no_progress` all mean "ended without achieving the goal" and were
// previously rendered as an ordinary assistant bubble — no warning, no
// recovery — because recognition stopped at `error`/`max_steps_reached`.
const FAILED_DONE_REASONS: ReadonlySet<string> = new Set([
  "error",
  "max_steps_reached",
  "unmet_goal",
  "verification_failed",
  "halted_no_progress",
]);

/**
 * Normalize a `completion` payload into {@link RunFailureMeta}, or null when
 * the run didn't fail. Degrades gracefully for events persisted before
 * `error_detail` shipped: the body falls back to the capped `error` /
 * `last_error` string and no classified title/provider is shown.
 *
 * A `blocked` run is the trap this guards against: the runtime deliberately
 * leaves `done_reason` at `completed` for a run that died against a
 * credential, a network path, a permission or quota, and carries the fact out
 * ONLY as `blocked_code`. Gating recognition on `done_reason` alone rendered
 * those as a green "Run completed" card. `blocked_code` is therefore consulted
 * independently of `done_reason`, and takes precedence when both are present —
 * a wall the user can act on is the more useful classification.
 */
export function parseRunFailure(
  payload: Record<string, unknown> | undefined,
): RunFailureMeta | null {
  const p = payload ?? {};
  const doneReason = readString(p, "done_reason")?.toLowerCase() ?? "";
  const blockedCode = readString(p, "blocked_code") || undefined;
  if (!blockedCode && !FAILED_DONE_REASONS.has(doneReason)) return null;

  const reason: RunFailureReason = blockedCode
    ? "blocked"
    : doneReason === "error"
      ? "error"
      : doneReason === "max_steps_reached"
        ? "max_steps_reached"
        : "unmet_goal";

  const detail = parseErrorDetail(p.error_detail);
  const legacy = readString(p, "error") ?? readString(p, "last_error") ?? "";
  // The classifier's reason and the attempted-model list ride the classified
  // detail when it is present, but a completion that carries them WITHOUT an
  // `error_detail` still has to surface them — recovery's model proposal is
  // the only consumer and it must not go dark on an unclassified failure.
  const rootTried = Array.isArray(p.models_tried) ? p.models_tried.map(String) : undefined;
  return {
    reason,
    text: detail?.detail || legacy,
    detail,
    failureReason: detail?.failure_reason ?? readString(p, "failure_reason"),
    modelsTried: detail?.models_tried ?? rootTried,
    blockedCode,
  };
}

/**
 * Normalize an `outcome_assertion` event payload into a `unmet_goal`
 * {@link RunFailureMeta}, or null when it carries no reason.
 *
 * This is the THIRD way a run is unmet, and the one a completion payload alone
 * cannot reveal: a session-end hook (the wiki "job never reached its terminal
 * tool" case) leaves `done_reason` at `completed` and appends this SEPARATE
 * event right after the completion it judges. `parseRunFailure`, reading the
 * completion in isolation, returns null for it — so both timeline builders
 * associate this event with the preceding completion instead. The payload is
 * `{reason, detail, source}`: `reason` is a product-owned token (drives the
 * recovery model default via `failureReason`, never a model switch since it is
 * not model-attributable), `detail` is the one-line body.
 */
export function parseOutcomeAssertion(
  payload: Record<string, unknown> | undefined,
): RunFailureMeta | null {
  const reason = readString(payload ?? {}, "reason");
  if (!reason) return null;
  return {
    reason: "unmet_goal",
    text: readString(payload ?? {}, "detail") ?? "",
    failureReason: reason,
  };
}

// ── Shared structured result parser ──────────────────────────────────
// Tool results may be JSON strings with a `kind` discriminator.
// Both buildLogs() and buildTimeline() use this to avoid duplication.

export type ParsedResult =
  | { kind: "diff"; title: string; text: string; files?: string[] }
  | {
      kind: "shell";
      command?: string;
      cwd?: string;
      exit_code?: number;
      stdout?: string;
      stderr?: string;
      duration_ms?: number;
    }
  | { kind: "file"; path: string; text: string; total_lines?: number }
  | {
      kind: "agent_tree";
      text: string;
      agents: AgentTreeNode[];
      parent_id: string;
      wait?: boolean;
      duration_ms?: number;
      waited_ms?: number;
    }
  | { kind: "raw"; text: string };

/** Heuristic: text contains unified diff markers (--- a/... and +++ b/...). */
function looksLikeUnifiedDiff(text: string): boolean {
  return /^---\s+\S/m.test(text) && /^\+\+\+\s+\S/m.test(text);
}

export function parseStructuredResult(result: unknown): ParsedResult {
  if (typeof result !== "string")
    return { kind: "raw", text: String(result ?? "") };
  try {
    const parsed = JSON.parse(result);
    if (parsed && typeof parsed === "object" && typeof parsed.kind === "string") {
      return parsed as ParsedResult;
    }
  } catch {
    /* not JSON */
  }
  // Detect unified diffs in raw text (e.g. from MCP file-write tools).
  if (looksLikeUnifiedDiff(result)) {
    return { kind: "diff", text: result, title: "File Change" };
  }
  return { kind: "raw", text: result };
}

// ── Existing exports ─────────────────────────────────────────────────

export type SummaryTesting = {
  summary: string[];
  testing: {
    command: string;
    passed: boolean;
  }[];
};

function formatToolInput(input: unknown): string {
  if (input === null || input === undefined) {
    return "";
  }
  if (typeof input === "string") {
    return input;
  }
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

function truncate(text: string, maxLen: number): string {
  return text.length > maxLen ? text.slice(0, maxLen) + "..." : text;
}

function parsePlanSteps(steps: unknown[]): PlanStep[] {
  return steps.map((step) => {
    if (typeof step === "string") {
      return { title: step };
    }
    if (step && typeof step === "object") {
      const typed = step as Record<string, unknown>;
      const title = readString(typed, "title") ?? readString(typed, "objective") ?? "Step";
      const description =
        readString(typed, "description") ?? readString(typed, "expected_output");
      return { title, description };
    }
    return { title: "Step" };
  });
}

function stepsEqual(a: PlanStep | undefined, b: PlanStep | undefined): boolean {
  if (!a || !b) {
    return false;
  }
  return a.title === b.title && (a.description || "") === (b.description || "");
}
// ── buildLogs — per-event-type builders + dispatch ────────────────────
// Each `buildXxxLog` mirrors LogsView.tsx's own `renderXxx` dispatch
// convention: one function per event type, returning the LogEntry/ies (or
// null/[] when the event produces no row), with a single switch at the
// bottom doing the dispatch. `nextId` is a closure over ONE shared counter
// (not per-type) so ids are byte-identical to the pre-split single loop.

type NextId = (prefix: string) => string;

interface AgentMaps {
  agentModelMap: Map<string, string>;
  agentTaskMap: Map<string, string>;
}

/**
 * Agent-id → model and agent-id → task maps from `sub_agent` start events
 * (first pass over the whole stream). The task map lets the steer_agent
 * builder resolve an agent_id prefix back to a full id + task description.
 */
function buildAgentMaps(events: EventRecord[]): AgentMaps {
  const agentModelMap = new Map<string, string>();
  const agentTaskMap = new Map<string, string>();
  for (const event of events) {
    if (event.type === "sub_agent") {
      const p = event.payload || {};
      if (p.action === "start" && typeof p.agent_id === "string") {
        if (typeof p.model === "string") {
          agentModelMap.set(p.agent_id as string, p.model as string);
        }
        if (typeof p.detail === "string") {
          agentTaskMap.set(p.agent_id as string, p.detail as string);
        }
      }
    }
  }
  return { agentModelMap, agentTaskMap };
}

/**
 * Resolve a (possibly truncated) agent_id prefix to a full {id, task} match
 * when exactly one full agent id starts with the prefix. Returns null when
 * ambiguous or absent.
 */
function resolveAgentPrefix(
  agentTaskMap: Map<string, string>,
  prefix: string,
): { id: string; task?: string } | null {
  if (!prefix) return null;
  if (agentTaskMap.has(prefix)) {
    return { id: prefix, task: agentTaskMap.get(prefix) };
  }
  const matches: string[] = [];
  for (const id of agentTaskMap.keys()) {
    if (id.startsWith(prefix)) matches.push(id);
  }
  if (matches.length === 1) {
    return { id: matches[0], task: agentTaskMap.get(matches[0]) };
  }
  return null;
}

interface ToolResultCtx {
  nextId: NextId;
  agentModelMap: Map<string, string>;
  agentTaskMap: Map<string, string>;
}

/**
 * `tool_result` fans out into one of several card shapes depending on
 * `tool_id` and the parsed `result` payload — spawn/agent bookkeeping, file
 * reads, steering messages, diffs, or the generic shell fallback. Returns
 * 0–2 entries (the imported-session "Agent" tool synthesizes both a
 * spawn_submit AND an agent_result from one event).
 */
function buildToolResultLogs(event: EventRecord, ctx: ToolResultCtx): LogEntry[] {
  const { nextId, agentModelMap, agentTaskMap } = ctx;
  const payload = event.payload || {};
  const toolId = readString(payload, "tool_id") ?? "tool";
  const operation = readString(payload, "operation") ?? "run";
  const rawInput = formatToolInput(payload.tool_input);
  const result = payload.result;
  const summary =
    payload.summary ||
    result ||
    payload.error ||
    "";
  const success = payload.success !== false;
  const eventAgentId = readString(payload, "agent_id");
  const eventModel =
    readString(payload, "model") ??
    (eventAgentId ? agentModelMap.get(eventAgentId) : undefined);

  // Parse AgentResult JSON from spawn_agent tool results.
  // Two payload shapes share tool_id="spawn_agent":
  //   - Blocking sub-agent: AgentResult dict (has steps_used).
  //   - Non-blocking root spawn: submit stub
  //     {agent_id, status:"submitted", task, message}.
  if (toolId === "spawn_agent" && typeof result === "string") {
    try {
      const ar = JSON.parse(result);
      if (ar.status && ar.steps_used !== undefined) {
        return [{
          id: nextId("agent-result"),
          type: "agent_result",
          content: "",
          timestamp: event.ts,
          agentResultStatus: String(ar.status),
          stepsUsed: Number(ar.steps_used) || 0,
          summary: typeof ar.summary === "string" ? truncate(ar.summary, 300) : undefined,
          artifacts: Array.isArray(ar.artifacts) ? ar.artifacts.map(String) : undefined,
          warnings: Array.isArray(ar.warnings) ? ar.warnings.map(String) : undefined,
        }];
      }
      if (typeof ar.agent_id === "string" && ar.status === "submitted") {
        const inp = (payload.tool_input as Record<string, unknown>) || {};
        const knownKeys = new Set([
          "task", "model", "allowed_tools", "denied_tools",
          "acceptance_criteria", "agent_type", "max_steps",
        ]);
        const extras: Array<[string, string]> = Object.entries(inp)
          .filter(([k]) => !knownKeys.has(k))
          .map(([k, v]) => [k, typeof v === "string" ? v : JSON.stringify(v)]);
        const taskInput = readString(inp, "task") ?? "";
        return [{
          id: nextId("spawn-submit"),
          type: "spawn_submit",
          content: "",
          timestamp: event.ts,
          spawnCaller: eventAgentId,
          spawnChildId: String(ar.agent_id),
          spawnTask: taskInput,
          spawnAgentType: readString(inp, "agent_type"),
          spawnModel: readString(inp, "model"),
          spawnAllowedTools: Array.isArray(inp.allowed_tools) ? inp.allowed_tools.map(String) : [],
          spawnDeniedTools: Array.isArray(inp.denied_tools) ? inp.denied_tools.map(String) : [],
          spawnAcceptance: readString(inp, "acceptance_criteria"),
          spawnExtras: extras,
          spawnMessage: readString(ar, "message") ?? "",
          spawnDurationMs: readNumber(payload, "duration_ms"),
        }];
      }
    } catch { /* not JSON, fall through to shell */ }
  }

  // Agent tool calls from imported sessions → spawn_submit (task) + agent_result (output)
  if (toolId === "Agent") {
    const inp = (payload.tool_input as Record<string, unknown>) || {};
    const description = readString(inp, "description") ?? "";
    const prompt = readString(inp, "prompt") ?? description;
    const subagentType = readString(inp, "subagent_type");
    const agentModel = readString(inp, "model");
    const resultText = typeof result === "string" ? result : "";
    const out: LogEntry[] = [{
      id: nextId("spawn-submit"),
      type: "spawn_submit",
      content: "",
      timestamp: event.ts,
      spawnTask: prompt,
      spawnAgentType: subagentType,
      spawnModel: agentModel,
      spawnChildId: "",
      spawnAllowedTools: [],
      spawnDeniedTools: [],
      spawnExtras: [],
      spawnMessage: "",
    }];
    if (resultText) {
      out.push({
        id: nextId("agent-result"),
        type: "agent_result",
        content: "",
        timestamp: event.ts,
        agentResultStatus: success ? "completed" : "failed",
        stepsUsed: 0,
        summary: truncate(resultText, 300),
      });
    }
    return out;
  }

  // Read tool calls from imported sessions → file_read card
  if (toolId === "Read") {
    const inp = (payload.tool_input as Record<string, unknown>) || {};
    const filePath = readString(inp, "file_path") ?? "";
    if (filePath) {
      return [{
        id: nextId("file-read"),
        type: "file_read",
        content: "",
        timestamp: event.ts,
        fileReadPath: filePath,
        fileReadText: typeof result === "string" ? result : "",
        agentId: eventAgentId,
        model: eventModel,
      }];
    }
  }

  // steer_agent → render as a chat line ("<root → agent-xxxxxx>").
  // The result is always a short string ("Message sent.", "Agent xxx
  // cancelled.", or "ERROR: ..."), so no JSON parse needed.
  if (toolId === "steer_agent") {
    const inp = (payload.tool_input as Record<string, unknown>) || {};
    const action = readString(inp, "action") ?? "";
    const targetPrefix = readString(inp, "agent_id") ?? "";
    const message = readString(inp, "message") ?? "";
    const steerResult = typeof result === "string" ? result : "";
    const resolved = resolveAgentPrefix(agentTaskMap, targetPrefix);
    const isError = steerResult.startsWith("ERROR");
    return [{
      id: nextId("steer"),
      type: "root_steer",
      content: message,
      timestamp: event.ts,
      steerAction: action,
      steerTargetPrefix: targetPrefix,
      steerTargetFullId: resolved?.id,
      steerTargetTask: resolved?.task,
      steerMessage: message,
      steerResult,
      steerIsError: isError,
    }];
  }

  // Parse structured result (diff, shell, or raw text)
  const parsedResult = parseStructuredResult(result);

  // check_agents → dedicated CheckAgentsCard with hi-fi tree + raw tab.
  if (toolId === "check_agents" && parsedResult.kind === "agent_tree") {
    return [{
      id: nextId("check-agents"),
      type: "check_agents",
      content: "",
      timestamp: event.ts,
      agents: parsedResult.agents,
      rawText: parsedResult.text,
      parentId: parsedResult.parent_id,
      wait: parsedResult.wait,
      durationMs: parsedResult.duration_ms,
      waitedMs: parsedResult.waited_ms,
    }];
  }

  // File read result → dedicated FileReadCard
  if (parsedResult.kind === "file") {
    return [{
      id: nextId("file-read"),
      type: "file_read",
      content: "",
      timestamp: event.ts,
      fileReadPath: parsedResult.path,
      fileReadText: parsedResult.text,
      fileReadTotalLines: parsedResult.total_lines,
      agentId: eventAgentId,
      model: eventModel,
    }];
  }

  // Diff result → dedicated diff card
  if (parsedResult.kind === "diff") {
    return [{
      id: nextId("diff"),
      type: "diff",
      content: "",
      timestamp: event.ts,
      diffTitle: parsedResult.title || toolId,
      diffText: parsedResult.text || "",
      diffSuccess: success,
      agentId: eventAgentId,
      model: eventModel,
    }];
  }

  // File edit tools without a structured diff result (typically failed
  // edits where result is null). Synthesize a diff from tool_input so
  // they render as DiffCard instead of a generic fallback.
  if (/edit|write|patch/i.test(toolId) && parsedResult.kind === "raw") {
    const inp = payload.tool_input as Record<string, unknown> | undefined;
    const filePath = readString(inp, "file_path") ?? "";
    if (filePath) {
      const oldStr = readString(inp, "old_string") ?? "";
      const newStr = readString(inp, "new_string") ?? readString(inp, "content") ?? "";
      let diffText = "";
      if (oldStr || newStr) {
        const oldSplit = oldStr ? oldStr.split("\n") : [];
        const newSplit = newStr ? newStr.split("\n") : [];
        const oldLines = oldSplit.map((l: string) => `-${l}`).join("\n");
        const newLines = newSplit.map((l: string) => `+${l}`).join("\n");
        const hunkHeader = `@@ -1,${oldSplit.length} +1,${newSplit.length} @@`;
        diffText = `--- ${filePath}\n+++ ${filePath}\n${hunkHeader}\n${[oldLines, newLines].filter(Boolean).join("\n")}`;
      }
      const errorMsg = readString(payload, "error") ?? "";
      return [{
        id: nextId("diff"),
        type: "diff",
        content: "",
        timestamp: event.ts,
        diffTitle: errorMsg ? `${filePath} — ${errorMsg}` : filePath,
        diffText: diffText || `(no diff available)`,
        diffSuccess: success,
        agentId: eventAgentId,
        model: eventModel,
      }];
    }
  }

  // Try to parse structured shell result
  let shellData: {
    command?: string; cwd?: string; exit_code?: number;
    stdout?: string; stderr?: string; duration_ms?: number;
  } | null = null;
  if (parsedResult.kind === "shell") {
    shellData = parsedResult;
  }

  // For shell tools without structured JSON (timeouts, internal errors),
  // synthesize shell fields from tool_input so TerminalCard still renders.
  const isShellTool = /shell|bash|exec|run_command/i.test(toolId);
  if (!shellData && isShellTool) {
    const inp = payload.tool_input;
    let cmd: string | undefined;
    if (inp && typeof inp === "object" && typeof (inp as Record<string, unknown>).command === "string") {
      cmd = (inp as Record<string, unknown>).command as string;
    } else if (typeof inp === "string") {
      cmd = inp.replace(/^\$\s*/, "");
    }
    if (cmd) {
      const errorMsg = !success ? String(payload.error || summary || "") : undefined;
      shellData = {
        command: cmd,
        cwd: readString(inp, "cwd"),
        exit_code: !success ? 1 : undefined,
        stdout: success ? (summary ? String(summary) : undefined) : undefined,
        stderr: errorMsg || undefined,
      };
    }
  }

  // Regular tool result → shell card with separated input/output.
  // For the OUTPUT body, prefer the raw `result` (full payload, capped
  // generously by the backend at EVENT_MAX_CHARS) over `summary`
  // (intentionally truncated for log-title use).
  const shellInput = shellData?.command || rawInput || undefined;
  const fullResult =
    typeof result === "string"
      ? result
      : result != null
        ? JSON.stringify(result)
        : undefined;
  const shellOutput =
    shellData?.stdout
    ?? fullResult
    ?? (summary ? String(summary) : undefined);
  const content = [
    shellInput ? `input: ${shellInput}` : "",
    shellOutput || "",
  ].filter(Boolean).join("\n\n");

  return [{
    id: nextId("tool"),
    type: "shell",
    title: `${toolId} (${operation})`,
    content,
    timestamp: event.ts,
    shellInput,
    shellOutput,
    error: !success ? String(payload.error || "Error") : undefined,
    agentId: eventAgentId,
    model: eventModel,
    shellCommand: shellData?.command,
    shellCwd: shellData?.cwd,
    shellExitCode: shellData?.exit_code,
    shellStdout: shellData?.stdout,
    shellStderr: shellData?.stderr,
    shellDurationMs: shellData?.duration_ms,
  }];
}

interface PlanDiffState {
  version: number;
  previousSteps: PlanStep[];
}

/** `action_plan` → a `plan` row, diffed against the previous version once a
 * second plan arrives. Mutates `state` in place (the running version number
 * + previous steps carry across calls, one state object per buildLogs run). */
function buildActionPlanLog(event: EventRecord, nextId: NextId, state: PlanDiffState): LogEntry {
  const steps = Array.isArray(event.payload?.steps)
    ? parsePlanSteps(event.payload?.steps)
    : [];
  state.version += 1;
  let planMode: PlanLogEntry["planMode"] = "full";
  let planSteps: PlanStep[] = steps;
  if (state.version > 1) {
    const diff: PlanStep[] = [];
    const maxLen = Math.max(state.previousSteps.length, steps.length);
    for (let i = 0; i < maxLen; i += 1) {
      const prev = state.previousSteps[i];
      const next = steps[i];
      if (prev && !next) {
        diff.push({ ...prev, diffType: "removed" });
        continue;
      }
      if (!prev && next) {
        diff.push({ ...next, diffType: "added" });
        continue;
      }
      if (prev && next && !stepsEqual(prev, next)) {
        diff.push({ ...next, diffType: "updated" });
      }
    }
    if (diff.length > 0) {
      planMode = "diff";
      planSteps = diff;
    }
  }
  const log: LogEntry = {
    id: nextId("plan"),
    type: "plan",
    content: "",
    steps: planSteps,
    version: state.version,
    label: state.version === 1 ? "Plan" : "Plan updated",
    planMode,
    timestamp: event.ts
  };
  state.previousSteps = steps;
  return log;
}

function buildStepReflectionLog(event: EventRecord, nextId: NextId): LogEntry {
  return {
    id: nextId("reflect"),
    type: "system",
    content: String(event.payload?.notes ?? "Step reflection updated."),
    timestamp: event.ts
  };
}

function buildContextCompactedLog(event: EventRecord, nextId: NextId): LogEntry {
  const p = event.payload || {};
  const saved = readNumber(p, "tokens_saved") ?? 0;
  const mode = readString(p, "mode") ?? "auto";
  const agentId = readString(p, "agent_id");
  const label = agentId
    ? `Agent [${agentId.slice(0, 8)}] context compacted (${mode})`
    : `Context compacted (${mode})`;
  // Summary is empty string when structured compaction failed (fallback).
  const rawSummary = typeof p.summary === "string" && p.summary.length > 0 ? p.summary : undefined;
  return {
    id: nextId("compact"),
    type: "compact",
    content: saved > 0 ? `${label} — ${saved.toLocaleString()} tokens freed` : label,
    timestamp: event.ts,
    compactSummary: rawSummary,
    tokensBefore: readNumber(p, "tokens_before"),
    tokensSaved: saved,
    tokensAfter: readNumber(p, "tokens_after"),
    eventsSummarized: readNumber(p, "events_summarized"),
    compactMode: mode,
    model: readString(p, "model"),
    agentId,
  };
}

function buildCompletionLog(event: EventRecord, nextId: NextId): LogEntry {
  const payload = event.payload || {};
  const done = payload.done ? "completed" : "incomplete";
  const reason = readString(payload, "done_reason") ?? "";
  return {
    id: nextId("completion"),
    type: "completion",
    content: `Run ${done}${reason ? ` (${reason})` : ""}.`,
    timestamp: event.ts,
    doneReason: reason || done,
    error: readString(payload, "error"),
    runFailure: parseRunFailure(payload) ?? undefined,
  };
}

/** Returns null for "allow" decisions — the tool_result card already
 * confirms execution; only deny/pending are worth a row. */
function buildPermissionLog(event: EventRecord, nextId: NextId): LogEntry | null {
  const payload = event.payload || {};
  const decision = readString(payload, "decision") ?? "pending";
  if (decision.toLowerCase() === "allow") return null;
  const toolId = readString(payload, "tool_id") ?? "tool";
  const operation = readString(payload, "operation") ?? "run";
  const rawInput = formatToolInput(payload.tool_input);
  return {
    id: nextId("permission"),
    type: "permission",
    content: `Permission ${decision}: ${toolId}`,
    timestamp: event.ts,
    decision,
    toolId,
    operation,
    toolInput: rawInput ? truncate(rawInput, 500) : undefined,
  };
}

function buildSubAgentLog(event: EventRecord, nextId: NextId): LogEntry {
  const payload = event.payload || {};
  const action = readString(payload, "action") ?? "event";
  const agentId = readString(payload, "agent_id") ?? "";
  const depth = readNumber(payload, "depth") ?? 0;
  const model = readString(payload, "model") ?? "";
  const detail = readString(payload, "detail") ?? "";
  const status = readString(payload, "status") ?? action;
  const steps = readNumber(payload, "steps_completed") ?? 0;
  const parentId = readString(payload, "parent_id");
  const inputTokens = readNumber(payload, "input_tokens");
  const outputTokens = readNumber(payload, "output_tokens");
  return {
    id: nextId("agent"),
    type: "agent",
    content: "",
    timestamp: event.ts,
    agentId,
    parentId,
    model,
    depth,
    agentAction: action,
    agentStatus: status,
    stepsCompleted: steps,
    inputTokens,
    outputTokens,
    detail: detail ? truncate(detail, 200) : undefined,
  };
}

function buildAgentMessageLog(event: EventRecord, nextId: NextId): LogEntry | null {
  const payload = event.payload || {};
  const text = readString(payload, "text") ?? "";
  const agentId = readString(payload, "agent_id") ?? "";
  const depth = readNumber(payload, "depth") ?? 0;
  if (!text) return null;
  return {
    id: nextId("msg"),
    type: "agent_message",
    content: text,
    timestamp: event.ts,
    agentId,
    depth,
    detail: depth === 0 ? "root" : `agent-${agentId.slice(0, 6)}`,
  };
}

function buildUserSteerLog(event: EventRecord, nextId: NextId): LogEntry | null {
  const text = readString(event.payload, "text") ?? "";
  if (!text) return null;
  return {
    id: nextId("steer"),
    type: "user_steer",
    content: text,
    timestamp: event.ts,
    detail: "user",
  };
}

// LLM resilience events — same-model retry, cross-model fallback, and the
// doom-loop halt. Surfaced inline in the telemetry lane so a flaky run is
// legible (why it slowed down / switched models / stopped).

function buildLlmRetryLog(event: EventRecord, nextId: NextId): LogEntry {
  const p = event.payload || {};
  return {
    id: nextId("llm-retry"),
    type: "llm_retry",
    content: "",
    timestamp: event.ts,
    model: readString(p, "model"),
    agentId: readString(p, "agent_id"),
    depth: readNumber(p, "depth"),
    retryAttempt: readNumber(p, "attempt"),
    retryMaxAttempts: readNumber(p, "max_attempts"),
    retryErrorType: readString(p, "error_type"),
    retryDelay: readNumber(p, "delay"),
    error: readString(p, "error"),
  };
}

function buildLlmFallbackLog(event: EventRecord, nextId: NextId): LogEntry {
  const p = event.payload || {};
  return {
    id: nextId("llm-fallback"),
    type: "llm_fallback",
    content: "",
    timestamp: event.ts,
    agentId: readString(p, "agent_id"),
    depth: readNumber(p, "depth"),
    fallbackFromModel: readString(p, "from_model"),
    fallbackToModel: readString(p, "to_model"),
    fallbackReason: readString(p, "reason"),
    fallbackPreviousErrorType: readString(p, "previous_error_type"),
    fallbackSticky: readBool(p, "sticky"),
  };
}

function buildRecoveryHaltLog(event: EventRecord, nextId: NextId): LogEntry {
  const p = event.payload || {};
  return {
    id: nextId("recovery-halt"),
    type: "recovery_halt",
    content: "",
    timestamp: event.ts,
    agentId: readString(p, "agent_id"),
    depth: readNumber(p, "depth"),
    recoveryAction: readString(p, "action"),
    recoveryTool: readString(p, "tool"),
  };
}

export function buildLogs(events: EventRecord[]): LogEntry[] {
  const logs: LogEntry[] = [];
  let idx = 0;
  const nextId: NextId = (prefix) => `${prefix}-${idx++}`;

  const { agentModelMap, agentTaskMap } = buildAgentMaps(events);
  const planState: PlanDiffState = { version: 0, previousSteps: [] };

  for (const event of events) {
    switch (event.type) {
      case "tool_result":
        logs.push(...buildToolResultLogs(event, { nextId, agentModelMap, agentTaskMap }));
        break;
      case "action_plan":
        logs.push(buildActionPlanLog(event, nextId, planState));
        break;
      // plan_proposed/plan_approved/plan_rejected events are rendered inline
      // in ConversationTimeline as PlanCard entries, not as log rows.
      case "step_reflection":
        logs.push(buildStepReflectionLog(event, nextId));
        break;
      case "context_compacted":
        logs.push(buildContextCompactedLog(event, nextId));
        break;
      case "completion":
        logs.push(buildCompletionLog(event, nextId));
        break;
      case "outcome_assertion": {
        // A hook's late verdict that the preceding run missed its goal. Attach
        // it to the most recent completion row (the event is appended right
        // after the completion it judges), UNLESS that run already classified
        // as a failure on its own — that read is more specific and stands.
        const meta = parseOutcomeAssertion(event.payload);
        if (meta) {
          for (let i = logs.length - 1; i >= 0; i -= 1) {
            const entry = logs[i];
            if (entry.type === "completion") {
              if (!entry.runFailure) entry.runFailure = meta;
              break;
            }
          }
        }
        break;
      }
      case "permission": {
        const log = buildPermissionLog(event, nextId);
        if (log) logs.push(log);
        break;
      }
      case "sub_agent":
        logs.push(buildSubAgentLog(event, nextId));
        break;
      case "agent_message": {
        const log = buildAgentMessageLog(event, nextId);
        if (log) logs.push(log);
        break;
      }
      case "user_steer": {
        const log = buildUserSteerLog(event, nextId);
        if (log) logs.push(log);
        break;
      }
      case "llm_retry":
        logs.push(buildLlmRetryLog(event, nextId));
        break;
      case "llm_fallback":
        logs.push(buildLlmFallbackLog(event, nextId));
        break;
      case "recovery":
        if (event.payload?.action === "halt_no_progress") {
          logs.push(buildRecoveryHaltLog(event, nextId));
        }
        break;
      default:
        break;
    }
  }
  return logs;
}
export function extractSummaryTesting(events: EventRecord[]): SummaryTesting {
  const summary: string[] = [];
  const testing: {
    command: string;
    passed: boolean;
  }[] = [];
  for (const event of events) {
    const payload = event.payload || {};
    if (event.type === "summary") {
      const text = payload.text;
      if (Array.isArray(text)) {
        for (const item of text) {
          if (typeof item === "string") {
            summary.push(item);
          }
        }
      } else if (typeof text === "string") {
        summary.push(text);
      }
    }
    if (event.type === "test_result") {
      const command = readString(payload, "command") ?? null;
      const passed = readBool(payload, "passed") ?? true;
      if (command) {
        testing.push({
          command,
          passed
        });
      }
    }
  }
  return {
    summary,
    testing
  };
}
