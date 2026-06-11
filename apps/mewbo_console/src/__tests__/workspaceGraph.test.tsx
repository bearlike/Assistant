/**
 * Workspace SCG graph view tests (#79).
 *
 * The graph view reuses the wiki ``KnowledgeGraphRenderer`` engine (cytoscape),
 * which jsdom can't boot — so the renderer is mocked at the module seam. These
 * assert the React shell around it: the fixture payload drives the stats strip,
 * the per-kind legend chips, the per-layer toggle, and the unmapped-ghost hint.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { WorkspaceGraphDialog } from "../components/agentic_search/graph/WorkspaceGraphDialog"
import * as api from "../api/agenticSearch"
import type { WorkspaceGraph } from "../components/agentic_search/graph/types"
import type { Workspace } from "../types/agenticSearch"

afterEach(cleanup)

// Mock cytoscape + fcose so the REAL renderer module loads (we keep makeIconUri
// / GraphRenderConfig intact) but its canvas calls are inert in jsdom. This
// avoids mocking the renderer module itself (and the import-hoist cycle that
// spreading it caused) — we test the React shell, not the cytoscape canvas.
vi.mock("cytoscape", () => {
  const fakeCy = () => ({
    on: vi.fn(),
    off: vi.fn(),
    destroy: vi.fn(),
    nodes: () => ({ forEach: vi.fn(), length: 0 }),
    edges: () => ({ forEach: vi.fn() }),
    elements: () => [],
    batch: (fn: () => void) => fn(),
    fit: vi.fn(),
    layout: () => ({ run: vi.fn() }),
    getElementById: () => ({ empty: () => true }),
  })
  return { default: Object.assign(fakeCy, { use: vi.fn() }) }
})
vi.mock("cytoscape-fcose", () => ({ default: {} }))

vi.mock("../api/agenticSearch", async () => {
  const actual = await vi.importActual<typeof import("../api/agenticSearch")>(
    "../api/agenticSearch",
  )
  return { ...actual, getWorkspaceGraph: vi.fn() }
})

const workspace: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github", "notion"],
  instructions: "",
  created: "May 2026",
  past_queries: [],
}

const fixture: WorkspaceGraph = {
  scope: ["github", "notion"],
  nodes: [
    { data: { id: "n1", label: "search", kind: "capability", layer: "schema", sourceId: "github", doc: "Search repos." } },
    { data: { id: "n2", label: "Repo", kind: "entity_type", layer: "schema", sourceId: "github" } },
    { data: { id: "m1", label: "Repo is queryable by id", kind: "Memory", layer: "memory", snippet: "Repo is queryable by id" } },
    { data: { id: "unmapped:notion", label: "notion", kind: "unmapped", layer: "schema", sourceId: "notion", unmapped: true } },
  ],
  edges: [
    { data: { id: "e1", source: "n1", target: "n2", kind: "PRODUCES", layer: "schema" } },
    { data: { id: "x1", source: "m1", target: "n2", kind: "ANCHORS", layer: "cross" } },
  ],
  stats: {
    totalNodes: 3,
    totalEdges: 2,
    kinds: { capability: 1, entity_type: 1, Memory: 1, unmapped: 1 },
    perLayer: { schema: 2, memory: 1, entity: 0 },
    unmapped: ["notion"],
  },
}

function renderDialog() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <WorkspaceGraphDialog open workspace={workspace} onClose={() => undefined} onMapSource={() => undefined} />
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.mocked(api.getWorkspaceGraph).mockResolvedValue(fixture)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe("WorkspaceGraphDialog (#79)", () => {
  it("renders the stats strip + per-kind legend from the fixture", async () => {
    renderDialog()
    // Per-kind legend chips (closed-union labels from the SCG config).
    expect(await screen.findByText("Capability")).toBeInTheDocument()
    expect(screen.getByText("Type")).toBeInTheDocument()
    // "Memory" appears as both a kind chip and a layer label — at least one.
    expect(screen.getAllByText("Memory").length).toBeGreaterThan(0)
    // The unmapped ghost kind shows in the legend (the #79 ghost affordance).
    expect(screen.getByText("Unmapped")).toBeInTheDocument()
    // The legend chip carries its per-kind count from stats.kinds.
    const capChip = screen.getByTitle("Hide Capability")
    expect(capChip).toHaveTextContent("1")
  })

  it("renders the per-layer toggle when more than one layer is present", async () => {
    renderDialog()
    // schema + memory both > 0 → the segmented layer control surfaces.
    expect(await screen.findByRole("group", { name: /toggle graph layers/i })).toBeInTheDocument()
    expect(screen.getByText("Capabilities")).toBeInTheDocument()
  })

  it("fetches the graph scoped to the workspace id", async () => {
    renderDialog()
    await screen.findByText("Capability")
    expect(api.getWorkspaceGraph).toHaveBeenCalledWith("w1")
  })
})
