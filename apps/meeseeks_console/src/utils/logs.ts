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
  for (const event of events) {
    if (event.type === "tool_result") {
      const payload = event.payload || {};
      const toolId =
        typeof payload.tool_id === "string" ? payload.tool_id : "tool";
      const operation =
        typeof payload.operation === "string" ? payload.operation : "run";
      const toolInput = formatToolInput(payload.tool_input);
      const result = payload.result;
      const summary =
        payload.summary ||
        result ||
        payload.error ||
        "";
      const detailLines = [];
      if (toolInput) {
        detailLines.push(`input: ${toolInput}`);
      }
      if (summary) {
        detailLines.push(String(summary));
      }
      const content = detailLines.join("\n\n");
      logs.push({
        id: `tool-${idx++}`,
        type: "shell",
        title: `${toolId} (${operation})`,
        content,
        timestamp: event.ts
      });
      if (toolId === "spawn_agent" && typeof result === "string") {
        try {
          const ar = JSON.parse(result);
          if (ar.status && ar.steps_used !== undefined) {
            const statusIcon = ar.status === "completed" ? "✅" :
                               ar.status === "failed" ? "❌" :
                               ar.status === "cannot_solve" ? "⚠" : "•";
            logs.push({
              id: `agent-result-${idx++}`,
              type: "system" as const,
              content: `${statusIcon} Sub-agent: ${ar.status} (${ar.steps_used} steps)${ar.summary ? "\n" + (ar.summary as string).slice(0, 200) : ""}`,
              timestamp: event.ts,
            });
          }
        } catch { /* not JSON, ignore */ }
      }
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
      const reason = payload.done_reason ? ` (${payload.done_reason})` : "";
      logs.push({
        id: `completion-${idx++}`,
        type: "system",
        content: `Run ${done}${reason}.`,
        timestamp: event.ts
      });
    }
    if (event.type === "permission") {
      const payload = event.payload || {};
      const toolId =
        typeof payload.tool_id === "string" ? payload.tool_id : "tool";
      const operation =
        typeof payload.operation === "string" ? payload.operation : "run";
      const toolInput = formatToolInput(payload.tool_input);
      const decision =
        typeof payload.decision === "string" ? payload.decision : "pending";
      const inputSegment = toolInput ? `, tool_input: ${toolInput}` : "";
      const icon = decision === "deny" ? "⛔" : decision === "allow" ? "✓" : "?";
      logs.push({
        id: `permission-${idx++}`,
        type: "system",
        content: `${icon} Permission ${decision}: ${toolId}:${operation}${inputSegment}`,
        timestamp: event.ts
      });
    }
    if (event.type === "sub_agent") {
      const payload = event.payload || {};
      const action = typeof payload.action === "string" ? payload.action : "event";
      const agentId = typeof payload.agent_id === "string" ? payload.agent_id : "";
      const depth = typeof payload.depth === "number" ? payload.depth : 0;
      const model = typeof payload.model === "string" ? payload.model : "";
      const detail = typeof payload.detail === "string" ? payload.detail : "";
      const indent = "\u00A0\u00A0".repeat(depth);
      const status = typeof payload.status === "string" ? payload.status : action;
      const steps = typeof payload.steps_completed === "number" ? payload.steps_completed : 0;
      const label = action === "start"
        ? `${indent}▶ Agent ${agentId} started (${model}): ${detail}`
        : `${indent}■ Agent ${agentId} ${status} (${steps} steps): ${detail}`;
      logs.push({
        id: `agent-${idx++}`,
        type: "system",
        content: label,
        timestamp: event.ts,
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
