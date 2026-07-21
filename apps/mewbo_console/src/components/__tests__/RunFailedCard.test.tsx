/**
 * RunFailedCard — the one "run failed" readout.
 *
 * Two invariants earn tests here. Recovery must be reachable WITHOUT expanding
 * the card (an upstream 502 page arrives as thousands of characters, and
 * burying Retry under it makes recovery a scavenger hunt), and only the latest
 * failure may offer it. The third is graceful degradation: events persisted
 * before `error_detail` shipped must still render, since the session page
 * replays full history on load.
 */
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RunFailedCard } from "../RunFailedCard";
import { LogsView } from "../LogsView";
import type { EventRecord, RunFailureMeta } from "../../types";

afterEach(cleanup);

const CLASSIFIED: RunFailureMeta = {
  reason: "error",
  text: "<html><body>502 Bad Gateway</body></html>",
  detail: {
    kind: "upstream_bad_gateway",
    title: "Upstream bad gateway",
    provider: "anthropic",
    detail: "<html><body>502 Bad Gateway</body></html>",
    detail_chars: 5887,
    truncated: true,
  },
};

function completion(payload: Record<string, unknown>, ts: string): EventRecord {
  return { ts, type: "completion", payload };
}

describe("RunFailedCard", () => {
  it("offers recovery while collapsed — the body stays hidden", () => {
    render(<RunFailedCard failure={CLASSIFIED} onRetry={vi.fn()} onContinue={vi.fn()} />);
    expect(screen.getByText("Retry")).toBeInTheDocument();
    expect(screen.getByText("Continue")).toBeInTheDocument();
    // Collapsed by default: the error text is not in the document at all.
    expect(screen.queryByText(/502 Bad Gateway/)).toBeNull();
  });

  it("recovering does not toggle the card open", () => {
    const onRetry = vi.fn();
    render(<RunFailedCard failure={CLASSIFIED} onRetry={onRetry} onContinue={vi.fn()} />);
    fireEvent.click(screen.getByText("Retry"));
    expect(onRetry).toHaveBeenCalledTimes(1);
    // The click must not bubble to LogEventCard's expand handler.
    expect(screen.queryByText(/502 Bad Gateway/)).toBeNull();
  });

  it("shows the classified title, provider chip and honest truncation count", () => {
    render(<RunFailedCard failure={CLASSIFIED} />);
    expect(screen.getByText("Run failed")).toBeInTheDocument();
    expect(screen.getByText("Upstream bad gateway")).toBeInTheDocument();
    expect(screen.getByText("anthropic")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Run failed"));
    expect(screen.getByText(/502 Bad Gateway/)).toBeInTheDocument();
    expect(
      screen.getByText("Showing first 41 of 5,887 characters"),
    ).toBeInTheDocument();
  });

  it("renders read-only without recovery handlers", () => {
    render(<RunFailedCard failure={CLASSIFIED} />);
    expect(screen.queryByText("Retry")).toBeNull();
    expect(screen.queryByText("Continue")).toBeNull();
  });

  it("degrades to the legacy error string when error_detail is absent", () => {
    render(
      <RunFailedCard failure={{ reason: "error", text: "boom" }} />,
    );
    expect(screen.getByText("Run failed")).toBeInTheDocument();
    // No classified title or provider chip to show.
    expect(screen.queryByText("Upstream bad gateway")).toBeNull();
    fireEvent.click(screen.getByText("Run failed"));
    expect(screen.getByText("boom")).toBeInTheDocument();
    expect(screen.queryByText(/Showing first/)).toBeNull();
  });

  it("renders header-only (no expander) when the failure carries no text", () => {
    render(<RunFailedCard failure={{ reason: "max_steps_reached", text: "" }} />);
    const title = screen.getByText("Task interrupted — step limit reached");
    fireEvent.click(title);
    expect(screen.queryByLabelText("Copy error detail")).toBeNull();
  });
});

describe("LogsView — only the newest completion offers recovery", () => {
  it("gives Retry/Continue to the latest failure, not an earlier one", () => {
    render(
      <LogsView
        events={[
          completion({ done: true, done_reason: "error", error: "first" }, "2026-04-05T10:00:00Z"),
          completion({ done: true, done_reason: "error", error: "second" }, "2026-04-05T11:00:00Z"),
        ]}
        onRetry={vi.fn()}
        onContinue={vi.fn()}
      />,
    );
    // Two failure cards, one recovery affordance.
    expect(screen.getAllByText("Run failed")).toHaveLength(2);
    expect(screen.getAllByText("Retry")).toHaveLength(1);
  });

  it("withholds recovery once a later run completed successfully", () => {
    render(
      <LogsView
        events={[
          completion({ done: true, done_reason: "error", error: "boom" }, "2026-04-05T10:00:00Z"),
          completion({ done: true, done_reason: "completed" }, "2026-04-05T11:00:00Z"),
        ]}
        onRetry={vi.fn()}
        onContinue={vi.fn()}
      />,
    );
    expect(screen.getByText("Run failed")).toBeInTheDocument();
    expect(screen.getByText("Run completed")).toBeInTheDocument();
    expect(screen.queryByText("Retry")).toBeNull();
  });

  it("renders a goal-not-met run as a failure, never a completed card", () => {
    render(
      <LogsView
        events={[
          completion(
            { done: true, done_reason: "verification_failed", error: "outcome assertion failed" },
            "2026-04-05T10:00:00Z",
          ),
        ]}
        onRetry={vi.fn()}
        onContinue={vi.fn()}
      />,
    );
    expect(screen.getByText("Goal not met")).toBeInTheDocument();
    expect(screen.queryByText("Run completed")).toBeNull();
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });

  it("renders a blocked run as a failure even though done_reason stays completed", () => {
    render(
      <LogsView
        events={[
          completion(
            { done: true, done_reason: "completed", blocked_code: "repo_access" },
            "2026-04-05T10:00:00Z",
          ),
        ]}
        onRetry={vi.fn()}
        onContinue={vi.fn()}
      />,
    );
    // The exact laundering this guards against: a wall must never read green.
    expect(screen.queryByText("Run completed")).toBeNull();
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    // Names the actionable cause without expanding the card.
    expect(screen.getByText(/repository access/)).toBeInTheDocument();
    expect(screen.getByText("Retry")).toBeInTheDocument();
  });
});
