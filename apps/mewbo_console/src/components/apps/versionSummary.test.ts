import { describe, expect, it } from "vitest";

import { describeVersionSummary } from "./versionSummary";
import type { AppVersionSummary } from "../../types/apps";

function summary(over: Partial<AppVersionSummary> = {}): AppVersionSummary {
  return {
    files_added: 0,
    files_changed: 0,
    files_removed: 0,
    pipelines_added: [],
    pipelines_removed: [],
    pipelines_changed: [],
    collections_added: [],
    collections_removed: [],
    ...over,
  };
}

describe("describeVersionSummary", () => {
  it("returns null when nothing changed (no fabricated placeholder)", () => {
    expect(describeVersionSummary(summary())).toBeNull();
  });

  it("singularizes a lone file change", () => {
    expect(describeVersionSummary(summary({ files_changed: 1 }))).toBe("1 file changed");
  });

  it("folds added/changed/removed into one file count", () => {
    expect(
      describeVersionSummary(summary({ files_added: 1, files_changed: 1, files_removed: 0 })),
    ).toBe("2 files changed");
  });

  it("composes files + added pipeline + added collection, joined by the bullet", () => {
    const line = describeVersionSummary(
      summary({
        files_changed: 2,
        pipelines_added: ["meetings"],
        collections_added: ["notes"],
      }),
    );
    expect(line).toBe("2 files changed · +1 pipeline (meetings) · +1 collection (notes)");
  });

  it("names multiple changed pipelines with the plural noun", () => {
    const line = describeVersionSummary(
      summary({ pipelines_changed: ["digest", "meetings"] }),
    );
    expect(line).toBe("2 pipelines changed (digest, meetings)");
  });

  it("renders a removed pipeline with a minus prefix", () => {
    const line = describeVersionSummary(summary({ pipelines_removed: ["stale-job"] }));
    expect(line).toBe("-1 pipeline (stale-job)");
  });
});
