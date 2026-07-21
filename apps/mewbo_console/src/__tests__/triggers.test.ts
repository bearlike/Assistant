import { describe, expect, it } from "vitest";
import {
  SessionTerminatedError,
  isActiveTrigger,
  isSessionTerminatedError,
  type TriggerDTO,
} from "../api/triggers";
import {
  KIND_META,
  STATUS_META,
  firesLabel,
  triggerArgsSummary,
} from "../components/triggers/triggerFormat";
import { buildTimeline } from "../utils/timeline";
import type { EventRecord } from "../types";

function ev(ts: string, type: string, payload: Record<string, unknown> = {}): EventRecord {
  return { ts, type, payload };
}

function trigger(overrides: Partial<TriggerDTO> = {}): TriggerDTO {
  return {
    id: "t1",
    session_id: "s1",
    kind: "time.cron",
    status: "armed",
    wake_prompt: "check the deploy",
    action: "message",
    args: {},
    fires: 0,
    created_at: "2026-07-13T10:00:00Z",
    created_by: "agent",
    ...overrides,
  };
}

describe("triggerFormat", () => {
  it("has an exhaustive, labelled KIND_META / STATUS_META", () => {
    for (const meta of Object.values(KIND_META)) {
      expect(meta.label).toBeTruthy();
      expect(meta.icon).toBeTruthy();
    }
    // Only `armed`/`paused` are live (still fire); terminals are not.
    expect(STATUS_META.armed.live).toBe(true);
    expect(STATUS_META.paused.live).toBe(true);
    expect(STATUS_META.cancelled.live).toBe(false);
    expect(STATUS_META.failed.color).toBe("red");
  });

  it("summarises kind-specific args", () => {
    expect(triggerArgsSummary({ kind: "time.cron", args: { cron: "0 9 * * *", tz: "UTC" } })).toBe(
      "0 9 * * * · UTC",
    );
    expect(triggerArgsSummary({ kind: "time.at", args: { at: "2026-07-14T09:00:00Z" } })).toBe(
      "2026-07-14T09:00:00Z",
    );
    expect(
      triggerArgsSummary({ kind: "ci.workflow", args: { repo: "o/r", workflow: "ci.yml" } }),
    ).toBe("o/r · ci.yml");
    expect(triggerArgsSummary({ kind: "webhook", args: {} })).toBe("");
  });

  it("formats the fires label with and without a cap", () => {
    expect(firesLabel({ fires: 3, max_fires: 5 })).toBe("3 / 5");
    expect(firesLabel({ fires: 3, max_fires: null })).toBe("3");
    expect(firesLabel({ fires: 0, max_fires: undefined })).toBe("0");
  });
});

describe("isActiveTrigger", () => {
  it("is true only for armed/paused", () => {
    expect(isActiveTrigger(trigger({ status: "armed" }))).toBe(true);
    expect(isActiveTrigger(trigger({ status: "paused" }))).toBe(true);
    expect(isActiveTrigger(trigger({ status: "cancelled" }))).toBe(false);
    expect(isActiveTrigger(trigger({ status: "completed" }))).toBe(false);
    expect(isActiveTrigger(trigger({ status: "expired" }))).toBe(false);
  });
});

describe("isSessionTerminatedError", () => {
  it("detects the typed error", () => {
    expect(isSessionTerminatedError(new SessionTerminatedError("gone"))).toBe(true);
  });
  it("detects the 410 envelope stringified into an Error message", () => {
    const err = new Error(
      JSON.stringify({ error: { code: "session_terminated", reason: "x", retryable: false } }),
    );
    expect(isSessionTerminatedError(err)).toBe(true);
  });
  it("detects a bare substring fallback", () => {
    expect(isSessionTerminatedError(new Error("rejected: session_terminated"))).toBe(true);
  });
  it("is false for unrelated errors", () => {
    expect(isSessionTerminatedError(new Error("network down"))).toBe(false);
    expect(isSessionTerminatedError(null)).toBe(false);
  });
});

describe("buildTimeline — trigger + termination markers", () => {
  it("renders trigger_armed as a mid-turn 'trigger' entry", () => {
    const entries = buildTimeline([
      ev("2026-07-13T10:00:00Z", "user", { text: "watch the PR" }),
      ev("2026-07-13T10:00:02Z", "trigger_armed", {
        trigger_id: "t1",
        kind: "forge.pr",
        summary: "o/r #12",
      }),
      ev("2026-07-13T10:00:05Z", "assistant", { text: "armed it" }),
    ]);
    const armed = entries.find((e) => e.role === "trigger");
    expect(armed).toBeDefined();
    expect(armed?.trigger).toMatchObject({ action: "armed", kind: "forge.pr", summary: "o/r #12" });
    expect(armed?.turnId).toBe("turn-1");
  });

  it("renders trigger_fired between turns (no open turn) instead of dropping it", () => {
    const entries = buildTimeline([
      ev("2026-07-13T10:00:00Z", "user", { text: "hi" }),
      ev("2026-07-13T10:00:05Z", "assistant", { text: "done" }),
      ev("2026-07-13T10:05:00Z", "trigger_fired", {
        trigger_id: "t1",
        kind: "time.cron",
        payload_summary: "cron tick",
      }),
    ]);
    const fired = entries.find((e) => e.role === "trigger");
    expect(fired).toBeDefined();
    expect(fired?.trigger).toMatchObject({ action: "fired", summary: "cron tick" });
  });

  it("renders session_terminated as a terminal divider entry", () => {
    const entries = buildTimeline([
      ev("2026-07-13T10:00:00Z", "user", { text: "hi" }),
      ev("2026-07-13T10:00:05Z", "assistant", { text: "done" }),
      ev("2026-07-13T11:00:00Z", "session_terminated", {}),
    ]);
    const last = entries[entries.length - 1];
    expect(last.role).toBe("session_terminated");
    expect(last.ts).toBe("2026-07-13T11:00:00Z");
  });
});
