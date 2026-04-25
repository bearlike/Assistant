import { AgentTreeNode, EventRecord, LogEntry, PlanStep, WidgetReadyEntry, WidgetReadyPayload } from "../types";

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
      const title =
        typeof typed.title === "string"
          ? typed.title
          : typeof typed.objective === "string"
          ? typed.objective
          : "Step";
      const description =
        typeof typed.description === "string"
          ? typed.description
          : typeof typed.expected_output === "string"
          ? typed.expected_output
          : undefined;
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
export function buildLogs(events: EventRecord[]): LogEntry[] {
  const logs: LogEntry[] = [];
  let idx = 0;
  let planVersion = 0;
  let previousSteps: PlanStep[] = [];

  // Build agent-id → model and agent-id → task maps from sub_agent start
  // events (first pass). The task map lets the steer_agent renderer resolve
  // an agent_id prefix back to a full id + task description.
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

  // Resolve a (possibly truncated) agent_id prefix to a full {id, task} match
  // when exactly one full agent id starts with the prefix. Returns null when
  // ambiguous or absent.
  const resolveAgentPrefix = (
    prefix: string,
  ): { id: string; task?: string } | null => {
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
  };

  for (const event of events) {
    if (event.type === "tool_result") {
      const payload = event.payload || {};
      const toolId =
        typeof payload.tool_id === "string" ? payload.tool_id : "tool";
      const operation =
        typeof payload.operation === "string" ? payload.operation : "run";
      const rawInput = formatToolInput(payload.tool_input);
      const result = payload.result;
      const summary =
        payload.summary ||
        result ||
        payload.error ||
        "";
      const success = payload.success !== false;
      const eventAgentId = typeof payload.agent_id === "string" ? payload.agent_id : undefined;
      const eventModel = typeof payload.model === "string"
        ? payload.model
        : eventAgentId ? agentModelMap.get(eventAgentId) : undefined;

      // Parse AgentResult JSON from spawn_agent tool results.
      // Two payload shapes share tool_id="spawn_agent":
      //   - Blocking sub-agent: AgentResult dict (has steps_used).
      //   - Non-blocking root spawn: submit stub
      //     {agent_id, status:"submitted", task, message}.
      if (toolId === "spawn_agent" && typeof result === "string") {
        try {
          const ar = JSON.parse(result);
          if (ar.status && ar.steps_used !== undefined) {
            logs.push({
              id: `agent-result-${idx++}`,
              type: "agent_result",
              content: "",
              timestamp: event.ts,
              agentResultStatus: String(ar.status),
              stepsUsed: Number(ar.steps_used) || 0,
              summary: typeof ar.summary === "string" ? truncate(ar.summary, 300) : undefined,
              artifacts: Array.isArray(ar.artifacts) ? ar.artifacts.map(String) : undefined,
              warnings: Array.isArray(ar.warnings) ? ar.warnings.map(String) : undefined,
            });
            continue;
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
            const taskInput = typeof inp.task === "string" ? inp.task : "";
            logs.push({
              id: `spawn-submit-${idx++}`,
              type: "spawn_submit",
              content: "",
              timestamp: event.ts,
              spawnCaller: eventAgentId,
              spawnChildId: String(ar.agent_id),
              spawnTask: taskInput,
              spawnAgentType: typeof inp.agent_type === "string" ? inp.agent_type : undefined,
              spawnModel: typeof inp.model === "string" ? inp.model : undefined,
              spawnAllowedTools: Array.isArray(inp.allowed_tools) ? inp.allowed_tools.map(String) : [],
              spawnDeniedTools: Array.isArray(inp.denied_tools) ? inp.denied_tools.map(String) : [],
              spawnAcceptance: typeof inp.acceptance_criteria === "string" ? inp.acceptance_criteria : undefined,
              spawnExtras: extras,
              spawnMessage: typeof ar.message === "string" ? ar.message : "",
              spawnDurationMs: typeof payload.duration_ms === "number" ? payload.duration_ms : undefined,
            });
            continue;
          }
        } catch { /* not JSON, fall through to shell */ }
      }

      // steer_agent → render as a chat line ("<root → agent-xxxxxx>").
      // The result is always a short string ("Message sent.", "Agent xxx
      // cancelled.", or "ERROR: ..."), so no JSON parse needed.
      if (toolId === "steer_agent") {
        const inp = (payload.tool_input as Record<string, unknown>) || {};
        const action = typeof inp.action === "string" ? inp.action : "";
        const targetPrefix = typeof inp.agent_id === "string" ? inp.agent_id : "";
        const message = typeof inp.message === "string" ? inp.message : "";
        const steerResult = typeof result === "string" ? result : "";
        const resolved = resolveAgentPrefix(targetPrefix);
        const isError = steerResult.startsWith("ERROR");
        logs.push({
          id: `steer-${idx++}`,
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
        });
        continue;
      }

      // Parse structured result (diff, shell, or raw text)
      const parsedResult = parseStructuredResult(result);

      // check_agents → dedicated CheckAgentsCard with hi-fi tree + raw tab.
      if (toolId === "check_agents" && parsedResult.kind === "agent_tree") {
        logs.push({
          id: `check-agents-${idx++}`,
          type: "check_agents",
          content: "",
          timestamp: event.ts,
          agents: parsedResult.agents,
          rawText: parsedResult.text,
          parentId: parsedResult.parent_id,
          wait: parsedResult.wait,
          durationMs: parsedResult.duration_ms,
          waitedMs: parsedResult.waited_ms,
        });
        continue;
      }

      // File read result → dedicated FileReadCard
      if (parsedResult.kind === "file") {
        logs.push({
          id: `file-read-${idx++}`,
          type: "file_read",
          content: "",
          timestamp: event.ts,
          fileReadPath: parsedResult.path,
          fileReadText: parsedResult.text,
          fileReadTotalLines: parsedResult.total_lines,
          agentId: eventAgentId,
          model: eventModel,
        });
        continue;
      }

      // Diff result → dedicated diff card
      if (parsedResult.kind === "diff") {
        logs.push({
          id: `diff-${idx++}`,
          type: "diff",
          content: "",
          timestamp: event.ts,
          diffTitle: parsedResult.title || toolId,
          diffText: parsedResult.text || "",
          diffSuccess: success,
          agentId: eventAgentId,
          model: eventModel,
        });
        continue;
      }

      // File edit tools without a structured diff result (typically failed
      // edits where result is null). Synthesize a diff from tool_input so
      // they render as DiffCard instead of a generic fallback.
      if (/edit|write|patch/i.test(toolId) && parsedResult.kind === "raw") {
        const inp = payload.tool_input as Record<string, unknown> | undefined;
        const filePath = typeof inp?.file_path === "string" ? inp.file_path : "";
        if (filePath) {
          const oldStr = typeof inp?.old_string === "string" ? inp.old_string : "";
          const newStr = typeof inp?.new_string === "string" ? inp.new_string : "";
          let diffText = "";
          if (oldStr || newStr) {
            const oldLines = oldStr ? oldStr.split("\n").map((l: string) => `-${l}`).join("\n") : "";
            const newLines = newStr ? newStr.split("\n").map((l: string) => `+${l}`).join("\n") : "";
            diffText = `--- ${filePath}\n+++ ${filePath}\n@@ edit @@\n${[oldLines, newLines].filter(Boolean).join("\n")}`;
          }
          const errorMsg = typeof payload.error === "string" ? payload.error : "";
          logs.push({
            id: `diff-${idx++}`,
            type: "diff",
            content: "",
            timestamp: event.ts,
            diffTitle: errorMsg ? `${filePath} — ${errorMsg}` : filePath,
            diffText: diffText || `(no diff available)`,
            diffSuccess: success,
            agentId: eventAgentId,
            model: eventModel,
          });
          continue;
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
            cwd: typeof (inp as Record<string, unknown>)?.cwd === "string"
              ? (inp as Record<string, unknown>).cwd as string : undefined,
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

      logs.push({
        id: `tool-${idx++}`,
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
      });
    }
    if (event.type === "action_plan") {
      const steps = Array.isArray(event.payload?.steps)
        ? parsePlanSteps(event.payload?.steps)
        : [];
      planVersion += 1;
      let planMode: LogEntry["planMode"] = "full";
      let planSteps: PlanStep[] = steps;
      if (planVersion > 1) {
        const diff: PlanStep[] = [];
        const maxLen = Math.max(previousSteps.length, steps.length);
        for (let i = 0; i < maxLen; i += 1) {
          const prev = previousSteps[i];
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
      logs.push({
        id: `plan-${idx++}`,
        type: "plan",
        content: "",
        steps: planSteps,
        version: planVersion,
        label: planVersion === 1 ? "Plan" : "Plan updated",
        planMode,
        timestamp: event.ts
      });
      previousSteps = steps;
    }
    // plan_proposed/plan_approved/plan_rejected events are rendered inline
    // in ConversationTimeline as PlanCard entries, not as log rows.
    if (event.type === "step_reflection") {
      logs.push({
        id: `reflect-${idx++}`,
        type: "system",
        content: String(event.payload?.notes ?? "Step reflection updated."),
        timestamp: event.ts
      });
    }
    if (event.type === "context_compacted") {
      const p = event.payload || {};
      const saved = typeof p.tokens_saved === "number" ? p.tokens_saved : 0;
      const mode = typeof p.mode === "string" ? p.mode : "auto";
      const agentId = typeof p.agent_id === "string" ? p.agent_id : undefined;
      const label = agentId
        ? `Agent [${agentId.slice(0, 8)}] context compacted (${mode})`
        : `Context compacted (${mode})`;
      // Summary is empty string when structured compaction failed (fallback).
      const rawSummary = typeof p.summary === "string" && p.summary.length > 0 ? p.summary : undefined;
      logs.push({
        id: `compact-${idx++}`,
        type: "compact",
        content: saved > 0 ? `${label} — ${saved.toLocaleString()} tokens freed` : label,
        timestamp: event.ts,
        compactSummary: rawSummary,
        tokensBefore: typeof p.tokens_before === "number" ? p.tokens_before : undefined,
        tokensSaved: saved,
        tokensAfter: typeof p.tokens_after === "number" ? p.tokens_after : undefined,
        eventsSummarized: typeof p.events_summarized === "number" ? p.events_summarized : undefined,
        compactMode: mode,
        model: typeof p.model === "string" ? p.model : undefined,
        agentId,
      });
    }
    if (event.type === "completion") {
      const payload = event.payload || {};
      const done = payload.done ? "completed" : "incomplete";
      const reason = typeof payload.done_reason === "string" ? payload.done_reason : "";
      logs.push({
        id: `completion-${idx++}`,
        type: "completion",
        content: `Run ${done}${reason ? ` (${reason})` : ""}.`,
        timestamp: event.ts,
        doneReason: reason || done,
        error: typeof payload.error === "string" ? payload.error : undefined,
      });
    }
    if (event.type === "permission") {
      const payload = event.payload || {};
      const decision =
        typeof payload.decision === "string" ? payload.decision : "pending";
      // Skip "allow" decisions — the tool_result card already confirms execution.
      // Only show deny/pending where human authority was actually exercised.
      if (decision.toLowerCase() === "allow") continue;
      const toolId =
        typeof payload.tool_id === "string" ? payload.tool_id : "tool";
      const operation =
        typeof payload.operation === "string" ? payload.operation : "run";
      const rawInput = formatToolInput(payload.tool_input);
      logs.push({
        id: `permission-${idx++}`,
        type: "permission",
        content: `Permission ${decision}: ${toolId}`,
        timestamp: event.ts,
        decision,
        toolId,
        operation,
        toolInput: rawInput ? truncate(rawInput, 500) : undefined,
      });
    }
    if (event.type === "sub_agent") {
      const payload = event.payload || {};
      const action = typeof payload.action === "string" ? payload.action : "event";
      const agentId = typeof payload.agent_id === "string" ? payload.agent_id : "";
      const depth = typeof payload.depth === "number" ? payload.depth : 0;
      const model = typeof payload.model === "string" ? payload.model : "";
      const detail = typeof payload.detail === "string" ? payload.detail : "";
      const status = typeof payload.status === "string" ? payload.status : action;
      const steps = typeof payload.steps_completed === "number" ? payload.steps_completed : 0;
      const parentId = typeof payload.parent_id === "string" ? payload.parent_id : undefined;
      const inputTokens = typeof payload.input_tokens === "number" ? payload.input_tokens : undefined;
      const outputTokens = typeof payload.output_tokens === "number" ? payload.output_tokens : undefined;
      logs.push({
        id: `agent-${idx++}`,
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
      });
    }
    if (event.type === "agent_message") {
      const payload = event.payload || {};
      const text = typeof payload.text === "string" ? payload.text : "";
      const agentId = typeof payload.agent_id === "string" ? payload.agent_id : "";
      const depth = typeof payload.depth === "number" ? payload.depth : 0;
      if (text) {
        logs.push({
          id: `msg-${idx++}`,
          type: "agent_message",
          content: text,
          timestamp: event.ts,
          agentId,
          depth,
          detail: depth === 0 ? "root" : `agent-${agentId.slice(0, 6)}`,
        });
      }
    }
    if (event.type === "user_steer") {
      const text = typeof event.payload?.text === "string" ? event.payload.text : "";
      if (text) {
        logs.push({
          id: `steer-${idx++}`,
          type: "user_steer",
          content: text,
          timestamp: event.ts,
          detail: "user",
        });
      }
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
      const command = typeof payload.command === "string" ? payload.command : null;
      const passed = typeof payload.passed === "boolean" ? payload.passed : true;
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

/**
 * Extract all widget_ready events from a session's event stream.
 * Returns entries in the order they were emitted.
 */
export function extractWidgetEvents(events: EventRecord[]): WidgetReadyEntry[] {
  return events
    .filter((e) => e.type === "widget_ready" && e.payload != null)
    .map((e) => ({
      type: "widget_ready" as const,
      ts: e.ts,
      payload: e.payload as unknown as WidgetReadyPayload,
    }));
}
