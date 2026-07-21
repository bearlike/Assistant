// Transcript-integrity contracts for `buildTimeline`.
//
// These are written from the CONSUMER's viewpoint — what `ConversationTimeline`
// must be able to put on screen — rather than from the shape of the parser.
// Each describe block covers one class of event that a real session produced
// and the transcript failed to render.
import { describe, expect, test } from "vitest";
import { EventRecord } from "../types";
import { buildTimeline, getActiveTurn } from "../utils/timeline";

function ev(
  ts: string,
  type: string,
  payload: Record<string, unknown> = {},
): EventRecord {
  return { ts, type, payload };
}

const DIFF_RESULT = [
  "--- a/app.py",
  "+++ b/app.py",
  "@@ -1,2 +1,2 @@",
  "-old",
  "+new",
].join("\n");

describe("buildTimeline — a turn interrupted before it ever concluded", () => {
  // The shape a real session produces when a run dies without writing EITHER
  // an `assistant` event or a `completion`: the next prompt is simply the next
  // thing in the transcript. Everything the run did in between used to vanish.
  const interruptedThenResumed: EventRecord[] = [
    ev("2026-04-05T10:00:00Z", "user", { text: "do the long thing" }),
    ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell", result: "step one" }),
    ev("2026-04-05T10:00:08Z", "tool_result", { tool_id: "shell", result: "step two" }),
    ev("2026-04-05T10:00:11Z", "tool_result", { tool_id: "file_edit", result: DIFF_RESULT }),
    // No assistant. No completion. Just the next prompt.
    ev("2026-04-05T10:05:00Z", "user", { text: "what happened?" }),
    ev("2026-04-05T10:05:09Z", "assistant", { text: "The run was cut short." }),
    ev("2026-04-05T10:05:10Z", "completion", { done: true, done_reason: "completed" }),
  ];

  test("the interrupted turn is rendered, not discarded", () => {
    const entries = buildTimeline(interruptedThenResumed);
    // user-1, the interrupted turn, user-2, assistant-2.
    expect(entries.map((e) => e.role)).toEqual([
      "user",
      "run_failed",
      "user",
      "assistant",
    ]);
    expect(entries[1].turnId).toBe("turn-1");
  });

  test("its body events survive onto the turn the trace panel reads", () => {
    const turn = buildTimeline(interruptedThenResumed)[1].turn;
    expect(turn).toBeDefined();
    expect(turn?.id).toBe("turn-1");
    // The opening user event + all three tool_results.
    expect(turn?.events).toHaveLength(4);
    expect(turn?.events.filter((e) => e.type === "tool_result")).toHaveLength(3);
  });

  test("its merged diffs stay reachable from the turn footer", () => {
    const turn = buildTimeline(interruptedThenResumed)[1].turn;
    expect(turn?.files.map((f) => f.path)).toContain("app.py");
  });

  test("no assistant prose is fabricated for a turn that produced none", () => {
    const entries = buildTimeline(interruptedThenResumed);
    expect(entries[1].content).toBe("");
    // The only assistant text anywhere is the one the model actually returned.
    expect(entries.filter((e) => e.content.trim().length > 0).map((e) => e.content))
      .toEqual(["do the long thing", "what happened?", "The run was cut short."]);
  });

  test("it is marked as never-concluded, distinct from a run that errored", () => {
    const failure = buildTimeline(interruptedThenResumed)[1].runFailure;
    expect(failure).toBeDefined();
    expect(failure?.reason).toBe("interrupted");
    // Nothing to show in the body — no done_reason, no error was ever recorded.
    expect(failure?.text).toBe("");
    expect(failure?.detail).toBeUndefined();
  });

  test("its token usage is computed from its own calls only", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "first" }),
      ev("2026-04-05T10:00:01Z", "llm_call_end", {
        depth: 0, input_tokens: 4_000, output_tokens: 40,
      }),
      ev("2026-04-05T10:01:00Z", "user", { text: "second" }),
      ev("2026-04-05T10:01:01Z", "llm_call_end", {
        depth: 0, input_tokens: 9_000, output_tokens: 90,
      }),
      ev("2026-04-05T10:01:05Z", "assistant", { text: "done" }),
    ]);
    expect(entries[1].turn?.tokenUsage?.inputTokens).toBe(4_000);
    expect(entries[3].turn?.tokenUsage?.inputTokens).toBe(9_000);
  });

  test("several interrupted turns in a row each render once", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "one" }),
      ev("2026-04-05T10:00:01Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:01:00Z", "user", { text: "two" }),
      ev("2026-04-05T10:01:01Z", "tool_result", { tool_id: "shell" }),
      ev("2026-04-05T10:02:00Z", "user", { text: "three" }),
      ev("2026-04-05T10:02:05Z", "assistant", { text: "finally" }),
    ]);
    expect(entries.map((e) => e.role)).toEqual([
      "user", "run_failed", "user", "run_failed", "user", "assistant",
    ]);
    expect(entries.map((e) => e.turnId)).toEqual([
      "turn-1", "turn-1", "turn-2", "turn-2", "turn-3", "turn-3",
    ]);
  });

  test("the LIVE trailing turn is left open for the in-flight rendering", () => {
    // The same unclosed shape, but with NO following user event — the session
    // is still running. Flushing this one would replace the streaming bubble
    // with a failure card while the model is still generating.
    const live: EventRecord[] = [
      ev("2026-04-05T10:00:00Z", "user", { text: "do it" }),
      ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell", result: "ok" }),
    ];
    const entries = buildTimeline(live);
    expect(entries.map((e) => e.role)).toEqual(["user"]);
    // And the active-turn walker still reports it as the open turn, which is
    // what gates PendingAssistantRow / StreamingAssistantRow.
    expect(getActiveTurn(live)?.id).toBe("turn-1");
  });

  test("a turn that DID conclude is never marked interrupted", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "one" }),
      ev("2026-04-05T10:00:05Z", "assistant", { text: "answer" }),
      ev("2026-04-05T10:01:00Z", "user", { text: "two" }),
      ev("2026-04-05T10:01:05Z", "assistant", { text: "answer two" }),
    ]);
    expect(entries.some((e) => e.runFailure?.reason === "interrupted")).toBe(false);
  });
});

describe("buildTimeline — recovery markers between turns", () => {
  // A retry/continue is recorded BETWEEN turns, so it always arrives with no
  // turn open. Anything gated on an open turn never sees one.
  test("a continue renders as its own marker entry", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "task" }),
      ev("2026-04-05T10:00:05Z", "assistant", { text: "(Run interrupted by error: x)" }),
      ev("2026-04-05T10:00:06Z", "completion", {
        done: true, done_reason: "error", error: "boom",
      }),
      ev("2026-04-05T10:02:00Z", "recovery", { action: "continue" }),
      ev("2026-04-05T10:02:01Z", "user", { text: "continue prompt" }),
      ev("2026-04-05T10:02:10Z", "assistant", { text: "recovered!" }),
    ]);
    expect(entries.map((e) => e.role)).toEqual([
      "user", "run_failed", "recovery", "user", "assistant",
    ]);
    expect(entries[2].recovery).toEqual({ action: "continue" });
    expect(entries[2].ts).toBe("2026-04-05T10:02:00Z");
  });

  test("a retry renders as a marker carrying its own action", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "task" }),
      ev("2026-04-05T10:00:06Z", "completion", { done: true, done_reason: "error" }),
      ev("2026-04-05T10:02:00Z", "recovery", { action: "retry" }),
      ev("2026-04-05T10:02:01Z", "user", { text: "task" }),
    ]);
    const marker = entries.find((e) => e.role === "recovery");
    expect(marker?.recovery?.action).toBe("retry");
  });

  test("the engine's own halt is not a conversation marker", () => {
    // `halt_no_progress` is emitted by the tool-use loop mid-turn. It belongs
    // in the trace, not in the chat, and it must stay on the turn's events so
    // the trace panel keeps rendering it.
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "task" }),
      ev("2026-04-05T10:00:05Z", "recovery", {
        action: "halt_no_progress", tool: "read_file",
      }),
      ev("2026-04-05T10:00:09Z", "assistant", { text: "stopped early" }),
    ]);
    expect(entries.some((e) => e.role === "recovery")).toBe(false);
    expect(
      entries[1].turn?.events.filter((e) => e.type === "recovery"),
    ).toHaveLength(1);
  });

  test("a recovery with no usable action creates nothing", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "task" }),
      ev("2026-04-05T10:00:06Z", "completion", { done: true, done_reason: "error" }),
      ev("2026-04-05T10:02:00Z", "recovery", {}),
    ]);
    expect(entries.some((e) => e.role === "recovery")).toBe(false);
  });
});

describe("buildTimeline — a failure completion must never destroy a real answer", () => {
  // The API's boot sweep appends a terminal completion to a run it found
  // orphaned. When the session died AFTER its assistant event but before its
  // completion, that sweep completion lands on a turn whose closure is the
  // model's REAL answer — not the orchestrator's placeholder.
  const sweptSession: EventRecord[] = [
    ev("2026-04-05T10:00:00Z", "user", { text: "summarise the report" }),
    ev("2026-04-05T10:00:05Z", "tool_result", { tool_id: "shell", result: "ok" }),
    ev("2026-04-05T10:00:09Z", "assistant", {
      text: "The report covers three regions and flags a shortfall in the second.",
    }),
    ev("2026-04-05T10:00:10Z", "completion", {
      done: true, done_reason: "error", error: "interrupted: process restart",
    }),
  ];

  test("the real answer survives verbatim", () => {
    const entries = buildTimeline(sweptSession);
    const assistant = entries.find((e) => e.role === "assistant");
    expect(assistant?.content).toBe(
      "The report covers three regions and flags a shortfall in the second.",
    );
  });

  test("the failure is surfaced alongside it, not over it", () => {
    const entries = buildTimeline(sweptSession);
    expect(entries.map((e) => e.role)).toEqual(["user", "assistant", "run_failed"]);
    expect(entries[2].runFailure).toMatchObject({
      reason: "error",
      text: "interrupted: process restart",
    });
  });

  test("only one turn footer renders — the meta stays on the answer", () => {
    const entries = buildTimeline(sweptSession);
    expect(entries[1].turn?.id).toBe("turn-1");
    expect(entries[2].turn).toBeUndefined();
  });

  test("the orchestrator's synthetic placeholder is still replaced in place", () => {
    // The common case must not regress: one entry per turn, no leftover bubble.
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "do it" }),
      ev("2026-04-05T10:00:09Z", "assistant", {
        text: "(Run interrupted by error: LLM call failed on all models)",
      }),
      ev("2026-04-05T10:00:10Z", "completion", {
        done: true, done_reason: "error", error: "LLM call failed on all models",
      }),
    ]);
    expect(entries.map((e) => e.role)).toEqual(["user", "run_failed"]);
    expect(entries[1].content).toBe("");
    expect(entries[1].turn?.id).toBe("turn-1");
  });

  test("every synthetic closure shape is recognised as a placeholder", () => {
    for (const [text, doneReason] of [
      ["(Run stopped: step limit reached before final answer)", "max_steps_reached"],
      ["(Run stopped: step budget exhausted before final answer)", "error"],
      ["(Run ended: unknown)", "error"],
      // A multi-line provider blob inside the closure clause still matches.
      ["(Run interrupted by error: 502\n<html>bad gateway</html>)", "error"],
    ] as const) {
      const entries = buildTimeline([
        ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
        ev("2026-04-05T10:00:09Z", "assistant", { text }),
        ev("2026-04-05T10:00:10Z", "completion", { done: true, done_reason: doneReason }),
      ]);
      expect(entries).toHaveLength(2);
      expect(entries[1].role).toBe("run_failed");
      expect(entries[1].content).toBe("");
    }
  });

  test("a real answer that merely mentions a run is not mistaken for a placeholder", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:09Z", "assistant", {
        text: "(Run the migration first.) Then restart the worker.",
      }),
      ev("2026-04-05T10:00:10Z", "completion", {
        done: true, done_reason: "error", error: "interrupted: process restart",
      }),
    ]);
    expect(entries[1].content).toBe(
      "(Run the migration first.) Then restart the worker.",
    );
    expect(entries[2].role).toBe("run_failed");
  });

  test("a successful completion still leaves a real answer untouched", () => {
    const entries = buildTimeline([
      ev("2026-04-05T10:00:00Z", "user", { text: "hi" }),
      ev("2026-04-05T10:00:09Z", "assistant", { text: "all done!" }),
      ev("2026-04-05T10:00:10Z", "completion", { done: true, done_reason: "completed" }),
    ]);
    expect(entries).toHaveLength(2);
    expect(entries[1]).toMatchObject({ role: "assistant", content: "all done!" });
  });
});
