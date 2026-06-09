/**
 * Render coverage for the QA answer view's provenance footer — the
 * secondary "Retrieval details" block surfacing the deterministic
 * `accessedSources` trail and the `modelsUsed` set from the answer
 * snapshot. Verifies citation-id shortening (scheme prefix stripped but
 * full id preserved in `title`), model labels, and that the whole block
 * is omitted when both trails are empty.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { ProvenanceFooter } from "@/components/wiki/QAScreen";

afterEach(cleanup);

describe("ProvenanceFooter", () => {
  it("renders nothing when both trails are empty", () => {
    const { container } = render(
      <ProvenanceFooter accessedSources={[]} modelsUsed={[]} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the accessed trail with scheme prefixes stripped for display", () => {
    render(
      <ProvenanceFooter
        accessedSources={[
          "graph:hypervisor.py::AgentHypervisor",
          "packages/mewbo_core/src/mewbo_core/config.py#L1-20",
          "wiki:core-orchestration",
        ]}
        modelsUsed={[]}
      />,
    );

    // graph:/wiki: prefixes dropped from the visible label…
    expect(
      screen.getByText("hypervisor.py::AgentHypervisor"),
    ).toBeInTheDocument();
    expect(screen.getByText("core-orchestration")).toBeInTheDocument();
    // …a bare path ref passes through verbatim.
    expect(
      screen.getByText(
        "packages/mewbo_core/src/mewbo_core/config.py#L1-20",
      ),
    ).toBeInTheDocument();

    // The unambiguous full id stays available on hover.
    expect(
      screen.getByTitle("graph:hypervisor.py::AgentHypervisor"),
    ).toBeInTheDocument();

    // The Models group is omitted when modelsUsed is empty.
    expect(screen.queryByText(/^Models$/)).toBeNull();
  });

  it("renders the distinct models that ran across the probes", () => {
    render(
      <ProvenanceFooter
        accessedSources={[]}
        modelsUsed={["openai/claude-sonnet-4-6", "openai/haiku"]}
      />,
    );

    // formatModelName strips the provider prefix for the chip label.
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    expect(screen.getByText("haiku")).toBeInTheDocument();

    // The Accessed group is omitted when accessedSources is empty.
    expect(screen.queryByText(/^Accessed$/)).toBeNull();
  });
});
