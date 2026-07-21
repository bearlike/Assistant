/**
 * HealthBody — the "declared collection never written" honesty signal.
 *
 * `freshness.unwritten_collections` is additive (spec: apps freshness-honesty
 * wave). An older `/system` payload simply lacks it and must render exactly
 * as it did before — the whole point of the field being optional.
 *
 * vitest runs WITHOUT globals → explicit cleanup (console convention).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { HealthBody } from "./Health";
import type { AppSystemHealth } from "../../../api/apps";
import type { AppFreshnessWire } from "../../../types/apps";

afterEach(cleanup);

function baseFreshness(over: Partial<AppFreshnessWire> = {}): AppFreshnessWire {
  return {
    last_success_at: "2026-07-17T09:00:08+00:00",
    last_run_status: "succeeded",
    next_fire_at: "2026-07-18T09:00:00+00:00",
    stale: false,
    ...over,
  };
}

function baseSystem(freshness: AppFreshnessWire): AppSystemHealth {
  return {
    app_id: "app-1",
    status: "live",
    freshness,
    triggers: [],
    runs: [],
    maintainer: { session_id: "s-maintainer", status: "active" },
    unscheduled_pipelines: [],
    pipelines: [],
  };
}

function renderHealth(system: AppSystemHealth | undefined, loading = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <HealthBody appId="app-1" system={system} loading={loading} />
    </QueryClientProvider>,
  );
}

describe("HealthBody — unwritten_collections honesty signal", () => {
  it("renders the 'Never written' line naming every declared collection the latest run skipped", () => {
    renderHealth(
      baseSystem(
        // The poster-child shape: the run succeeded (stale=false, has a
        // last_success_at) yet a declared collection got nothing.
        baseFreshness({ unwritten_collections: ["records"] }),
      ),
    );
    expect(screen.getByText("Never written: records")).toBeInTheDocument();
    // Still reads "just now"/formatted, not flagged stale — this signal is
    // additive to, not a replacement for, the existing stale row.
    expect(screen.queryByText("not scheduled")).not.toBeInTheDocument();
  });

  it("joins multiple unwritten collection names", () => {
    renderHealth(
      baseSystem(baseFreshness({ unwritten_collections: ["records", "audit_log"] })),
    );
    expect(screen.getByText("Never written: records, audit_log")).toBeInTheDocument();
  });

  it("renders no 'Never written' line when the field is an empty list", () => {
    renderHealth(baseSystem(baseFreshness({ unwritten_collections: [] })));
    expect(screen.queryByText(/Never written/)).not.toBeInTheDocument();
  });

  it("renders exactly as before when an older server omits the field entirely", () => {
    // No `unwritten_collections` key at all — the pre-this-wave wire shape.
    renderHealth(baseSystem(baseFreshness()));
    expect(screen.queryByText(/Never written/)).not.toBeInTheDocument();
    // The rest of the strip still renders normally.
    expect(screen.getByText("Last refreshed")).toBeInTheDocument();
    expect(screen.getByText("Maintainer")).toBeInTheDocument();
  });
});
