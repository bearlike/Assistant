import { describe, expect, test } from "vitest";
import { EventRecord } from "../types";
import { buildTimeline, getActiveTurn, turnHasWidget } from "../utils/timeline";
import { buildLogs } from "../utils/logs";

// Minimal helper — only fields the timeline builder reads.
function ev(
  ts: string,
  type: string,
  payload: Record<string, unknown> = {},
): EventRecord {
  return { ts, type, payload };
}

describe("buildTimeline — completion fallback materialisation", () => {
  test("failed turn (no assistant event) is materialised via completion", () => {
    // Turn 1: user + tool_results + completion(error). No assistant event —
    // this is exactly the shape of a run that died mid-flight. Without the
    // defensive fallback, turn 1's tool_results were silently dropped.
    const events: EventRecord[] = [
      ev("2026-04-05T10:00:00Z", "user", { text: "do the thing" }),
      ev("2026-04-05T10:00:05Z", "tool_result", {
        tool_id: "shell", result: "ok",
      }),
      ev("2026-04-05T10:00:08Z", "tool_result", {
        tool_id: "shell", result: "more",
      }),
      ev("2026-04-05T10:05:00Z", "completion", {
        done: true, done_reason: "error", error: "boom",
      }),
    ];
    const entries = buildTimeline(events);
    // User entry + synthetic assistant closure entry.
    expect(entries).toHaveLength(2);
    expect(entries[0]).toMatchObject({ role: "user", turnId: "turn-1" });
    expect(entries[1]).toMatchObject({
      role: "assistant",
      turnId: "turn-1",
      content: "(run interrupted — see logs)",
    });
    // TurnMeta attached with the failed turn's events intact:
    // user + 2 tool_results + completion (turnEvents seeds with user at line 15).
    const turn = entries[1].turn;
    expect(turn).toBeDefined();
    expect(turn?.events).toHaveLength(4);
    expect(turn?.events.filter(e => e.type === "tool_result")).toHaveLength(2);
    expect(turn?.events.filter(e => e.type === "completion")).toHaveLength(1);
  });

  test("max_steps_reached completion produces correct closure label", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:05:00Z", "completion", {
        done: true, done_reason: "max_steps_reached",
      }),
    ]);
    expect(entries[1].content).toBe("(step limit reached)");
  });

  test("canceled completion produces correct closure label", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:05:00Z", "completion", {
        done: true, done_reason: "canceled",
      }),
    ]);
    expect(entries[1].content).toBe("(run canceled)");
  });

  test("real assistant event wins — completion does NOT double-materialise", () => {
    // Turn 1 closes on its real assistant event. A subsequent completion
    // event (emitted after the assistant) must NOT create a second entry.
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:02Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:00:05Z", "assistant", { text: "done!" }),
      ev("2026-04-05T10:00:06Z", "completion", {
        done: true, done_reason: "completed",
      }),
    ]);
    expect(entries).toHaveLength(2);
    expect(entries[1].content).toBe("done!");
    expect(entries[1].id).toBe("assistant-1");
  });

  test("failed-then-recovered two-turn session materialises both turns", () => {
    // Turn 1: failed (no assistant, just completion(error)).
    // Turn 2: recovery user + tool_result + real assistant + completion.
    // Both turns must materialise; the recovery/completion events between
    // them must not leak into turn 1 or turn 2.
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "original task" }),
      ev("2026-04-05T10:00:02Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:00:04Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:01:00Z", "completion", {
        done: true, done_reason: "error", error: "timeout",
      }),
      ev("2026-04-05T10:02:00Z", "recovery", { action: "continue" }),
      ev("2026-04-05T10:02:01Z", "user", { text: "continue prompt" }),
      ev("2026-04-05T10:02:05Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:02:10Z", "assistant", { text: "recovered!" }),
      ev("2026-04-05T10:02:11Z", "completion", {
        done: true, done_reason: "completed",
      }),
    ]);
    // 2 users + 2 assistants (one synthetic, one real) = 4 entries.
    expect(entries).toHaveLength(4);
    expect(entries[0]).toMatchObject({ role: "user", turnId: "turn-1" });
    expect(entries[1]).toMatchObject({
      role: "assistant",
      turnId: "turn-1",
      content: "(run interrupted — see logs)",
    });
    expect(entries[2]).toMatchObject({ role: "user", turnId: "turn-2" });
    expect(entries[3]).toMatchObject({
      role: "assistant",
      turnId: "turn-2",
      content: "recovered!",
    });
    // Turn 1 carries user + 2 tool_results + completion = 4 events.
    expect(entries[1].turn?.events).toHaveLength(4);
    expect(
      entries[1].turn?.events.filter(e => e.type === "tool_result"),
    ).toHaveLength(2);
    // Turn 2 carries user + tool_result + assistant = 3 events (assistant
    // IS pushed before materialisation, it's just the closer signal).
    expect(entries[3].turn?.events).toHaveLength(3);
    expect(
      entries[3].turn?.events.filter(e => e.type === "tool_result"),
    ).toHaveLength(1);
  });

  test("completion without an open turn is ignored (no orphan entry)", () => {
    // A stray completion event at the very start of a transcript must
    // not emit a turn entry (there is no turn to close).
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "completion", {
        done: true, done_reason: "error",
      }),
      ev("2026-04-05T10:00:01Z", "user", { text: "hi" }),
    ]);
    expect(entries).toHaveLength(1);
    expect(entries[0].role).toBe("user");
  });
});

describe("buildTimeline — per-turn model from context events", () => {
  test("each turn carries the model from its preceding context event", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "context", { model: "openai/gpt-4o" }),
      ev("2026-04-05T10:00:01Z", "user", { text: "first" }),
      ev("2026-04-05T10:00:05Z", "assistant", { text: "reply 1" }),
      ev("2026-04-05T10:01:00Z", "context", { model: "anthropic/claude-sonnet" }),
      ev("2026-04-05T10:01:01Z", "user", { text: "second" }),
      ev("2026-04-05T10:01:05Z", "assistant", { text: "reply 2" }),
    ]);
    expect(entries).toHaveLength(4);
    expect(entries[1].turn?.model).toBe("openai/gpt-4o");
    expect(entries[3].turn?.model).toBe("anthropic/claude-sonnet");
  });

  test("turn without preceding context event has undefined model", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:05Z", "assistant", { text: "hello" }),
    ]);
    expect(entries[1].turn?.model).toBeUndefined();
  });

  test("completion fallback turn also carries per-turn model", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "context", { model: "openai/gpt-4o" }),
      ev("2026-04-05T10:00:01Z", "user", { text: "do it" }),
      ev("2026-04-05T10:00:05Z", "completion", { done: true, done_reason: "error" }),
    ]);
    expect(entries[1].turn?.model).toBe("openai/gpt-4o");
  });
});

describe("getActiveTurn — per-turn model from context events", () => {
  test("active turn carries model from preceding context event", () => {
    const active = getActiveTurn([
      ev("2026-04-05T10:00:00Z", "context", { model: "openai/gpt-4o" }),
      ev("2026-04-05T10:00:01Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:02Z", "tool_result", { tool_id: "shell" }),
    ]);
    expect(active).not.toBeNull();
    expect(active?.model).toBe("openai/gpt-4o");
  });
});

describe("getActiveTurn — completion closes the active turn", () => {
  test("completion event clears active turn state", () => {
    const events: EventRecord[] = [
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:02Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:01:00Z", "completion", {
        done: true, done_reason: "error",
      }),
    ];
    expect(getActiveTurn(events)).toBeNull();
  });

  test("active turn without completion is queryable", () => {
    const events: EventRecord[] = [
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:02Z", "tool_result", { tool_id: "shell" }),
    ];
    const active = getActiveTurn(events);
    expect(active).not.toBeNull();
    expect(active?.id).toBe("turn-1");
    expect(active?.events).toHaveLength(2);
  });
});

describe("user_steer events — steering messages in logs, not timeline", () => {
  test("user_steer does NOT create a new turn in the conversation timeline", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "initial query" }),
      ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell", result: "ok" }),
      ev("2026-04-05T10:00:10Z", "user_steer", { text: "focus on tests" }),
      ev("2026-04-05T10:00:15Z", "tool_result", { tool_id: "shell", result: "ok" }),
      ev("2026-04-05T10:00:20Z", "assistant", { text: "done" }),
    ]);
    const userEntries = entries.filter(e => e.role === "user");
    expect(userEntries).toHaveLength(1);
    expect(userEntries[0].content).toBe("initial query");
  });

  test("user_steer appears as a log entry via buildLogs", () => {
    const logs = buildLogs([
      ev("2026-04-05T10:00:00Z", "user", { text: "initial" }),
      ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell", result: "ok" }),
      ev("2026-04-05T10:00:10Z", "user_steer", { text: "steer me please" }),
    ]);
    const steerLogs = logs.filter(l => l.type === "user_steer");
    expect(steerLogs).toHaveLength(1);
    expect(steerLogs[0].content).toBe("steer me please");
    expect(steerLogs[0].detail).toBe("user");
    expect(steerLogs[0].timestamp).toBe("2026-04-05T10:00:10Z");
  });

  test("multiple user_steer events each produce a log entry", () => {
    const logs = buildLogs([
      ev("2026-04-05T10:00:00Z", "user", { text: "initial" }),
      ev("2026-04-05T10:00:05Z", "user_steer", { text: "first steer" }),
      ev("2026-04-05T10:00:10Z", "user_steer", { text: "second steer" }),
    ]);
    const steerLogs = logs.filter(l => l.type === "user_steer");
    expect(steerLogs).toHaveLength(2);
    expect(steerLogs[0].content).toBe("first steer");
    expect(steerLogs[1].content).toBe("second steer");
  });

  test("empty user_steer text is skipped", () => {
    const logs = buildLogs([
      ev("2026-04-05T10:00:00Z", "user_steer", { text: "" }),
    ]);
    expect(logs.filter(l => l.type === "user_steer")).toHaveLength(0);
  });

  test("interrupt event renders as user_steer log entry", () => {
    const logs = buildLogs([
      ev("2026-04-05T10:00:00Z", "user_steer", { text: "[Interrupted by user]" }),
    ]);
    const steerLogs = logs.filter(l => l.type === "user_steer");
    expect(steerLogs).toHaveLength(1);
    expect(steerLogs[0].content).toContain("Interrupted");
  });

});

describe("context_compacted events — compaction log parsing", () => {
  test("context_compacted emits compact log type with enriched fields", () => {
    const logs = buildLogs([
      ev("2026-04-16T10:00:00Z", "context_compacted", {
        mode: "auto",
        tokens_before: 42000,
        tokens_saved: 33800,
        tokens_after: 8200,
        events_summarized: 23,
        summary: "The user asked about compaction transparency.",
      }),
    ]);
    const compactLogs = logs.filter(l => l.type === "compact");
    expect(compactLogs).toHaveLength(1);
    const log = compactLogs[0];
    expect(log.compactSummary).toBe("The user asked about compaction transparency.");
    expect(log.tokensBefore).toBe(42000);
    expect(log.tokensSaved).toBe(33800);
    expect(log.tokensAfter).toBe(8200);
    expect(log.eventsSummarized).toBe(23);
    expect(log.compactMode).toBe("auto");
  });

  test("context_compacted without summary falls back gracefully", () => {
    const logs = buildLogs([
      ev("2026-04-16T10:00:00Z", "context_compacted", {
        mode: "auto",
        tokens_saved: 500,
      }),
    ]);
    const compactLogs = logs.filter(l => l.type === "compact");
    expect(compactLogs).toHaveLength(1);
    expect(compactLogs[0].compactSummary).toBeUndefined();
    expect(compactLogs[0].tokensBefore).toBeUndefined();
    expect(compactLogs[0].tokensAfter).toBeUndefined();
    expect(compactLogs[0].eventsSummarized).toBeUndefined();
    expect(compactLogs[0].tokensSaved).toBe(500);
  });
});

// ---------------------------------------------------------------------------
// Turn token usage — peak vs sum semantics.
//
// Regression tests for the "root took 120K in" display bug. Within a single
// turn the root prompt GROWS as tool results stack, so summing input_tokens
// across calls double-counts the baseline once per call. The footer must
// report the peak (max) — the real context-pressure signal.
// ---------------------------------------------------------------------------

describe("buildTimeline — turn token usage (peak for input, sum for output)", () => {
  test("root input uses max, not sum, across multiple LLM calls", () => {
    // A multi-step tool-use turn where the prompt grows 13K → 27K as tool
    // results append. Summing gives a meaningless ~100K+ "input" number.
    const events: EventRecord[] = [
      ev("2026-04-17T10:00:00Z", "user", { text: "research task" }),
      ev("2026-04-17T10:00:01Z", "llm_call_end", {
        depth: 0, input_tokens: 13_080, output_tokens: 200,
      }),
      ev("2026-04-17T10:00:05Z", "llm_call_end", {
        depth: 0, input_tokens: 18_000, output_tokens: 180,
      }),
      ev("2026-04-17T10:00:09Z", "llm_call_end", {
        depth: 0, input_tokens: 27_039, output_tokens: 300,
      }),
      ev("2026-04-17T10:00:10Z", "assistant", { text: "done" }),
    ];
    const entries = buildTimeline(events);
    const turn = entries.find(e => e.role === "assistant")?.turn;
    expect(turn).toBeDefined();
    const usage = turn?.tokenUsage;
    expect(usage).toBeDefined();
    // PEAK for input, not sum.
    expect(usage?.inputTokens).toBe(27_039);
    expect(usage?.inputTokens).toBeLessThan(13_080 + 18_000 + 27_039);
    // Output is additive.
    expect(usage?.outputTokens).toBe(200 + 180 + 300);
  });

  test("sub-agent input is sum of per-agent peaks (parallel isolated contexts)", () => {
    // Two sub-agents; each grows in its own isolated context. The combined
    // "peak pressure" is sum of per-agent maxes — NOT sum of every call.
    const events: EventRecord[] = [
      ev("2026-04-17T10:00:00Z", "user", { text: "delegate" }),
      // Root call (counted as root peak).
      ev("2026-04-17T10:00:01Z", "llm_call_end", {
        depth: 0, input_tokens: 10_000, output_tokens: 100,
      }),
      // sub1 grows 5K → 15K.
      ev("2026-04-17T10:00:02Z", "llm_call_end", {
        depth: 1, agent_id: "sub1", input_tokens: 5_000, output_tokens: 100,
      }),
      ev("2026-04-17T10:00:03Z", "llm_call_end", {
        depth: 1, agent_id: "sub1", input_tokens: 15_000, output_tokens: 100,
      }),
      // sub2 grows 3K → 6K.
      ev("2026-04-17T10:00:04Z", "llm_call_end", {
        depth: 1, agent_id: "sub2", input_tokens: 3_000, output_tokens: 50,
      }),
      ev("2026-04-17T10:00:05Z", "llm_call_end", {
        depth: 1, agent_id: "sub2", input_tokens: 6_000, output_tokens: 50,
      }),
      ev("2026-04-17T10:00:06Z", "assistant", { text: "done" }),
    ];
    const entries = buildTimeline(events);
    const turn = entries.find(e => e.role === "assistant")?.turn;
    const usage = turn?.tokenUsage;
    expect(usage?.inputTokens).toBe(10_000);  // single root call
    // sub1 peak 15K + sub2 peak 6K = 21K. Not 5+15+3+6 = 29K.
    expect(usage?.subInputTokens).toBe(15_000 + 6_000);
    expect(usage?.subAgentCount).toBe(2);
    // Sub-agent output is still summed.
    expect(usage?.subOutputTokens).toBe(100 + 100 + 50 + 50);
  });

  test("single root call — peak equals the only value", () => {
    // Edge case: one call per turn. Peak and sum are trivially equal here;
    // this guards against the bug being 'patched' by accidentally dropping
    // single-call turns entirely.
    const events: EventRecord[] = [
      ev("2026-04-17T10:00:00Z", "user", { text: "2+2?" }),
      ev("2026-04-17T10:00:01Z", "llm_call_end", {
        depth: 0, input_tokens: 12_238, output_tokens: 380,
      }),
      ev("2026-04-17T10:00:02Z", "assistant", { text: "4" }),
    ];
    const turn = buildTimeline(events).find(e => e.role === "assistant")?.turn;
    expect(turn?.tokenUsage?.inputTokens).toBe(12_238);
    expect(turn?.tokenUsage?.outputTokens).toBe(380);
  });
});

describe("agent_message events — chat label in buildLogs", () => {
  test("root agent (depth=0) is labelled 'root', not 'truss'", () => {
    const logs = buildLogs([
      ev("2026-04-22T10:00:00Z", "agent_message", {
        text: "hello from root",
        agent_id: "aaaa-bbbb-cccc",
        depth: 0,
      }),
    ]);
    const msg = logs.filter(l => l.type === "agent_message");
    expect(msg).toHaveLength(1);
    expect(msg[0].detail).toBe("root");
  });

  test("sub-agent (depth=1) is labelled with truncated agent id", () => {
    const logs = buildLogs([
      ev("2026-04-22T10:00:01Z", "agent_message", {
        text: "hello from sub",
        agent_id: "dddd-eeee-ffff",
        depth: 1,
      }),
    ]);
    const msg = logs.filter(l => l.type === "agent_message");
    expect(msg).toHaveLength(1);
    expect(msg[0].detail).toBe("agent-dddd-e");
  });
});

describe("buildTimeline — widget_ready events render inline in the turn", () => {
  test("widget_ready inside an active turn produces a widget timeline entry", () => {
    const widgetPayload = {
      widget_id: "w1",
      session_id: "s1",
      files: { "app.py": "import streamlit as st", "data.json": "{}" },
      requirements: ["plotly"],
      summary: "demo",
    };
    const entries = buildTimeline([
      ev("2026-04-23T08:00:00Z", "user", { text: "build a widget" }),
      ev("2026-04-23T08:00:05Z", "widget_ready", widgetPayload),
      ev("2026-04-23T08:00:10Z", "assistant", { text: "done" }),
    ]);

    const widgetEntries = entries.filter((e) => e.role === "widget");
    expect(widgetEntries).toHaveLength(1);
    expect(widgetEntries[0].widget).toEqual(widgetPayload);
    expect(widgetEntries[0].turnId).toBe("turn-1");

    // Widget entry must land between the user entry and the assistant entry
    // so it renders inline inside the turn, not before or after.
    const roles = entries.map((e) => e.role);
    expect(roles).toEqual(["user", "widget", "assistant"]);
  });

  test("widget_ready outside of an open turn is ignored (no orphan entry)", () => {
    // No `user` event to open a turn — widget should be dropped.
    const entries = buildTimeline([
      ev("2026-04-23T08:00:00Z", "widget_ready", {
        widget_id: "w1",
        session_id: "s1",
        files: { "app.py": "", "data.json": "{}" },
        requirements: [],
      }),
    ]);
    expect(entries).toEqual([]);
  });

  test("multiple widget_ready events in one turn all render", () => {
    const entries = buildTimeline([
      ev("2026-04-23T08:00:00Z", "user", { text: "build two" }),
      ev("2026-04-23T08:00:05Z", "widget_ready", {
        widget_id: "first",
        session_id: "s1",
        files: { "app.py": "", "data.json": "{}" },
        requirements: [],
      }),
      ev("2026-04-23T08:00:06Z", "widget_ready", {
        widget_id: "second",
        session_id: "s1",
        files: { "app.py": "", "data.json": "{}" },
        requirements: [],
      }),
      ev("2026-04-23T08:00:10Z", "assistant", { text: "done" }),
    ]);
    const widgets = entries.filter((e) => e.role === "widget");
    expect(widgets.map((e) => e.widget?.widget_id)).toEqual(["first", "second"]);
  });
});

describe("turnHasWidget", () => {
  const widgetPayload = {
    widget_id: "w1",
    session_id: "s1",
    files: { "app.py": "", "data.json": "{}" },
    requirements: [],
  };

  test("returns true when a widget entry shares the turnId", () => {
    const timeline = buildTimeline([
      ev("2026-04-25T10:00:00Z", "user", { text: "make me a widget" }),
      ev("2026-04-25T10:00:05Z", "widget_ready", widgetPayload),
      ev("2026-04-25T10:00:10Z", "assistant", { text: "done" }),
    ]);
    expect(turnHasWidget(timeline, "turn-1")).toBe(true);
  });

  test("returns false for a turn that has no widget entry", () => {
    const timeline = buildTimeline([
      ev("2026-04-25T10:00:00Z", "user", { text: "no widget here" }),
      ev("2026-04-25T10:00:10Z", "assistant", { text: "done" }),
    ]);
    expect(turnHasWidget(timeline, "turn-1")).toBe(false);
  });

  test("returns false for a turnId that does not exist", () => {
    const timeline = buildTimeline([
      ev("2026-04-25T10:00:00Z", "user", { text: "hello" }),
      ev("2026-04-25T10:00:05Z", "widget_ready", widgetPayload),
      ev("2026-04-25T10:00:10Z", "assistant", { text: "done" }),
    ]);
    expect(turnHasWidget(timeline, "turn-99")).toBe(false);
  });

  test("isolates widgets to their own turn in a multi-turn timeline", () => {
    const timeline = buildTimeline([
      ev("2026-04-25T10:00:00Z", "user", { text: "first" }),
      ev("2026-04-25T10:00:05Z", "widget_ready", widgetPayload),
      ev("2026-04-25T10:00:10Z", "assistant", { text: "ok" }),
      ev("2026-04-25T10:01:00Z", "user", { text: "second" }),
      ev("2026-04-25T10:01:10Z", "assistant", { text: "ok" }),
    ]);
    expect(turnHasWidget(timeline, "turn-1")).toBe(true);
    expect(turnHasWidget(timeline, "turn-2")).toBe(false);
  });
});
