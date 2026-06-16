/**
 * Landing health-band tests (#139).
 *
 * The active-workspace health band must read the LIGHT
 * `GET /workspaces/<id>/graph/summary` projection — NOT the full node/edge
 * graph (which stays lazy on the dialog). These assert the band fetches the
 * summary, renders its stats, and never pulls the full graph on landing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { LandingPanel } from "../components/agentic_search/LandingPanel"
import * as api from "../api/agenticSearch"
import type { WorkspaceGraphSummary } from "../components/agentic_search/graph/types"
import type { Workspace } from "../types/agenticSearch"

afterEach(cleanup)

vi.mock("../api/agenticSearch", async (orig) => {
  const actual = await orig<typeof import("../api/agenticSearch")>()
  return {
    ...actual,
    getWorkspaceGraph: vi.fn(),
    getWorkspaceGraphSummary: vi.fn(),
    listWorkspaceRuns: vi.fn(),
  }
})

const workspace: Workspace = {
  id: "w1",
  name: "Platform",
  desc: "Infra and CI",
  sources: ["github", "notion"],
  instructions: "",
  created: "today",
  past_queries: [],
}

const summary: WorkspaceGraphSummary = {
  scope: ["github", "notion"],
  stats: {
    totalNodes: 5,
    totalEdges: 3,
    kinds: { capability: 3, entity_type: 2 },
    perLayer: { schema: 5, memory: 2, entity: 0 },
    unmapped: ["notion"], // 1 of 2 sources mapped
  },
}

function renderLanding() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LandingPanel
        workspace={workspace}
        workspaces={[workspace]}
        sources={[]}
        tier="auto"
        onTierChange={vi.fn()}
        model=""
        onModelChange={vi.fn()}
        onPickWorkspace={vi.fn()}
        onSubmit={vi.fn()}
        onOpenCreate={vi.fn()}
        onOpenConfig={vi.fn()}
        onOpenSources={vi.fn()}
        onOpenRun={vi.fn()}
        onOpenGraph={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(api.getWorkspaceGraphSummary).mockResolvedValue(summary)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe("WorkspaceHealthBand summary read (#139)", () => {
  it("fetches the light summary for the active workspace, not the full graph", async () => {
    renderLanding()
    // mapped = total(2) − unmapped(1) = 1 → "1/2 sources mapped".
    expect(await screen.findByText("1/2")).toBeInTheDocument()
    expect(api.getWorkspaceGraphSummary).toHaveBeenCalledWith("w1")
    // The full node/edge graph is NEVER fetched on the landing — it stays lazy
    // behind the capability-graph dialog.
    expect(api.getWorkspaceGraph).not.toHaveBeenCalled()
  })

  it("renders node·edge size and memory-note count from the summary stats", async () => {
    renderLanding()
    expect(await screen.findByText("5·3")).toBeInTheDocument() // totalNodes·totalEdges
    expect(screen.getByText("2")).toBeInTheDocument() // perLayer.memory notes
  })
})
