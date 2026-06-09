/**
 * LogsView — sticky-pin transparency on the cross-model fallback card (Part E).
 *
 * When the engine pins the destination model for the rest of the run
 * (``sticky: true`` on the ``llm_fallback`` event), the fallback card shows a
 * "Pinned for run" badge so the user sees the switch is now the active model.
 * A non-sticky fallback shows only the "Fallback" badge.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { LogsView } from "../LogsView";
import type { EventRecord } from "../../types";

function fallbackEvent(sticky?: boolean): EventRecord {
  return {
    ts: "2026-06-08T00:00:00Z",
    type: "llm_fallback",
    payload: {
      from_model: "openai/gpt-4o-mini",
      to_model: "anthropic/claude-3-5-sonnet",
      reason: "quota_exhausted",
      depth: 0,
      ...(sticky === undefined ? {} : { sticky }),
    },
  };
}

afterEach(cleanup);

describe("LogsView — llm_fallback sticky pin", () => {
  it("renders the 'Pinned for run' badge when the switch is sticky", () => {
    render(<LogsView events={[fallbackEvent(true)]} />);
    expect(screen.getByText("Pinned for run")).toBeInTheDocument();
    expect(screen.getByText("Fallback")).toBeInTheDocument();
  });

  it("omits the pin badge for a non-sticky fallback", () => {
    render(<LogsView events={[fallbackEvent(false)]} />);
    expect(screen.getByText("Fallback")).toBeInTheDocument();
    expect(screen.queryByText("Pinned for run")).toBeNull();
  });

  it("omits the pin badge when the event has no sticky field", () => {
    render(<LogsView events={[fallbackEvent()]} />);
    expect(screen.queryByText("Pinned for run")).toBeNull();
  });
});
