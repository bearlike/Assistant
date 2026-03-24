import { EventRecord, LogEntry, PlanStep } from "../types";
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

  // Build agent-id → model map from sub_agent start events (first pass)
  const agentModelMap = new Map<string, string>();
  for (const event of events) {
    if (event.type === "sub_agent") {
      const p = event.payload || {};
      if (p.action === "start" && typeof p.agent_id === "string" && typeof p.model === "string") {
        agentModelMap.set(p.agent_id as string, p.model as string);
      }
    }
  }

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
      const eventModel = eventAgentId ? agentModelMap.get(eventAgentId) : undefined;

      // Parse AgentResult JSON from spawn_agent tool results
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
        } catch { /* not JSON, fall through to shell */ }
      }

      // Regular tool result → shell card with separated input/output
      const shellInput = rawInput || undefined;
      const shellOutput = summary ? String(summary) : undefined;
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
    if (event.type === "step_reflection") {
      logs.push({
        id: `reflect-${idx++}`,
        type: "system",
        content: String(event.payload?.notes ?? "Step reflection updated."),
        timestamp: event.ts
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
        detail: detail ? truncate(detail, 200) : undefined,
      });
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
