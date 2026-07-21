/**
 * Version-row rendering — the summary line + verification badges are
 * additive/optional wire fields (spec: apps update-loop wave). An older
 * version row simply lacks them and must render exactly as it did before.
 *
 * vitest runs WITHOUT globals → explicit cleanup (console convention).
 */
import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { VersionsBody } from "./railSections/Versions";
import type { AppSpec, AppVersion } from "../../types/apps";

afterEach(cleanup);

function baseSpec(over: Partial<AppSpec> = {}): AppSpec {
  return {
    app_id: "app-1",
    title: "Meeting Digest",
    summary: "Summarizes meetings",
    icon: "📝",
    owner_session_id: "s-1",
    workspace_ref: { kind: "own", key: "app-1" },
    frontend: { entrypoint: "app.py", files: { "app.py": "" }, requirements: [] },
    collections: [],
    pipelines: [],
    policies: { on_pipeline_failure: "notify", retention_days: null, max_docs_per_collection: 1000 },
    maintainer_session_id: null,
    version: 1,
    status: "live",
    created_at: "2026-07-01T00:00:00Z",
    updated_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function version(over: Partial<AppVersion> = {}): AppVersion {
  const v = over.version ?? 1;
  return {
    app_id: "app-1",
    version: v,
    spec: baseSpec({ version: v }),
    author: "user",
    created_at: "2026-07-01T00:00:00Z",
    ...over,
  };
}

function renderVersions(versions: AppVersion[], activeVersion = 1) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VersionsBody appId="app-1" activeVersion={activeVersion} versions={versions} />
    </QueryClientProvider>,
  );
}

describe("VersionsBody — additive summary + verification", () => {
  it("renders the compact summary line and per-pipeline verification badges when present", () => {
    renderVersions([
      version({
        version: 2,
        summary: {
          files_added: 0,
          files_changed: 2,
          files_removed: 0,
          pipelines_added: ["meetings"],
          pipelines_removed: [],
          pipelines_changed: [],
          collections_added: [],
          collections_removed: [],
        },
        verification: { meetings: "pass", digest: "fail" },
      }),
    ], 2);

    expect(screen.getByText("2 files changed · +1 pipeline (meetings)")).toBeInTheDocument();
    expect(screen.getByText(/meetings/, { selector: "span" })).toBeInTheDocument();
    expect(screen.getByText(/digest/, { selector: "span" })).toBeInTheDocument();
  });

  it("renders an older row with no summary/verification exactly as before (no placeholder)", () => {
    renderVersions([version({ version: 1, note: "Initial build" })], 1);

    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("Initial build")).toBeInTheDocument();
    // No fabricated "0 files changed" line, no stray verification badge.
    expect(screen.queryByText(/files changed/)).not.toBeInTheDocument();
    expect(screen.queryByText(/pipeline/)).not.toBeInTheDocument();
  });
});
