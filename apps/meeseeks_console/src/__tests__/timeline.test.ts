import { describe, expect, test } from "vitest";
import { EventRecord } from "../types";
import { buildTimeline, getActiveTurn } from "../utils/timeline";

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
