/**
 * Workspace SCG graph dialog tests — post graph-3d unification.
 *
 * ``WorkspaceGraphDialog`` no longer forks a Cytoscape renderer: it injects the
 * search-domain ``SCG_GRAPH_THEME`` + a node inspector into the SHARED 3D
 * ``Graph3DView`` engine. ``react-force-graph-3d`` pulls three.js/WebGL (unusable
 * in jsdom) so it is mocked at the module seam into a flat list of clickable node
 * buttons — clicking one fires ``onNodeClick(node)`` exactly as the real engine
 * does, which opens the SCG ``NodeInspector``. ``useWorkspaceGraph`` is mocked to
 * hand back a fixture graph so no network / QueryClient is needed.
 *
 * These assert REAL behaviour: the toolbar stats line reflects
 * ``stats.totalNodes/totalEdges``; clicking a mapped node opens the inspector
 * with that node's label + doc + connections; an all-unmapped workspace surfaces
 * the "map a source" banner whose "Open Sources" button calls ``onMapSource``.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"

import { WorkspaceGraphDialog } from "../components/agentic_search/graph/WorkspaceGraphDialog"
import type { WorkspaceGraph } from "../components/agentic_search/graph/types"
import { useWorkspaceGraph } from "../hooks/useAgenticSearch"
import type { Workspace } from "../types/agenticSearch"

afterEach(cleanup)

// ── Mock the WebGL renderer ─────────────────────────────────────────────────
// A forwardRef stand-in (the real engine is handed a ``ref``) that renders each
// flattened node datum as a button wired to ``onNodeClick`` and honours
// ``nodeVisibility`` — enough to drive picks + verify kind filtering in jsdom.
vi.mock("react-force-graph-3d", async () => {
  const React = await import("react")
  type FgProps = {
    graphData?: { nodes?: Array<{ id: string }> }
    nodeVisibility?: (n: unknown) => boolean
    onNodeClick?: (n: unknown) => void
  }
  const Mock = React.forwardRef<unknown, FgProps>(function ForceGraph3DMock(props, _ref) {
    const nodes = props.graphData?.nodes ?? []
    return (
      <div data-testid="force-graph">
        {nodes
          .filter((n) => props.nodeVisibility?.(n) !== false)
          .map((n) => (
            <button
              key={n.id}
              type="button"
              data-testid={`node-${n.id}`}
              onClick={() => props.onNodeClick?.(n)}
            >
              {n.id}
            </button>
          ))}
      </div>
    )
  })
  return { default: Mock }
})

// ``useWorkspaceGraph`` is the dialog's ONLY server-state seam — stub it so the
// graph payload is synchronous (no fetch, no QueryClientProvider).
vi.mock("../hooks/useAgenticSearch", () => ({ useWorkspaceGraph: vi.fn() }))

// <ForceGraph3D> only mounts once the canvas box measures non-zero; jsdom reports
// 0 for clientWidth/Height, so force a real size.
beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 600 })
})

// ── Fixtures ─────────────────────────────────────────────────────────────────

const workspace: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github", "notion"],
  instructions: "",
  created: "May 2026",
  past_queries: [],
}

/** A mixed mapped graph: a capability → type schema edge, an anchored memory
 *  note, and one unmapped ghost source. */
const mappedGraph: WorkspaceGraph = {
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

/** A workspace whose every source is still a ghost — the "map a source" path. */
const allUnmappedGraph: WorkspaceGraph = {
  scope: ["github", "notion"],
  nodes: [
    { data: { id: "unmapped:github", label: "github", kind: "unmapped", layer: "schema", sourceId: "github", unmapped: true } },
    { data: { id: "unmapped:notion", label: "notion", kind: "unmapped", layer: "schema", sourceId: "notion", unmapped: true } },
  ],
  edges: [],
  stats: {
    totalNodes: 0,
    totalEdges: 0,
    kinds: { unmapped: 2 },
    perLayer: { schema: 2, memory: 0, entity: 0 },
    unmapped: ["github", "notion"],
  },
}

type WgQuery = ReturnType<typeof useWorkspaceGraph>

/** A settled (non-pending, non-error) query result carrying ``graph``. */
function settled(graph: WorkspaceGraph): WgQuery {
  return { data: graph, isPending: false, isError: false, isSuccess: true } as unknown as WgQuery
}

function renderDialog(graph: WorkspaceGraph, onMapSource = vi.fn()) {
  vi.mocked(useWorkspaceGraph).mockReturnValue(settled(graph))
  const utils = render(
    <WorkspaceGraphDialog
      open
      onOpenChange={vi.fn()}
      workspace={workspace}
      onMapSource={onMapSource}
    />,
  )
  return { ...utils, onMapSource }
}

afterEach(() => {
  vi.clearAllMocks()
})

describe("WorkspaceGraphDialog — graph-3d unified", () => {
  it("scopes the graph query to the workspace id", async () => {
    renderDialog(mappedGraph)
    await screen.findByTestId("force-graph")
    expect(useWorkspaceGraph).toHaveBeenCalledWith("w1")
  })

  it("renders the toolbar stats line from stats.totalNodes/totalEdges", async () => {
    renderDialog(mappedGraph)
    await screen.findByTestId("force-graph")
    // The tight stats wrapper whose ENTIRE text is the counts line — anchored so
    // ancestors (which add 'Node types', layer chips…) and the digit children
    // don't also match. Pins the exact numbers the BE stats carry.
    const statsLine = screen.getByText((_content, el) => {
      const norm = el?.textContent?.replace(/\s+/g, " ").trim() ?? ""
      return /^\d+ nodes·\d+ edges$/.test(norm)
    })
    expect(statsLine.textContent?.replace(/\s+/g, " ").trim()).toBe("3 nodes·2 edges")
  })

  it("opens the NodeInspector with the clicked node's label, doc + connections", async () => {
    renderDialog(mappedGraph)
    // No inspector before a pick.
    expect(screen.queryByRole("complementary")).toBeNull()

    fireEvent.click(await screen.findByTestId("node-n1"))

    const inspector = screen.getByRole("complementary")
    // Header: human kind label + the node's own label.
    expect(within(inspector).getByText("Capability")).toBeInTheDocument()
    expect(within(inspector).getByText("search")).toBeInTheDocument()
    // Body: the capability doc string + its source.
    expect(within(inspector).getByText("Search repos.")).toBeInTheDocument()
    expect(within(inspector).getByText("github")).toBeInTheDocument()
    // 1-hop adjacency resolved via the shared GraphIndex: n1 --PRODUCES--> Repo.
    expect(within(inspector).getByText(/Outgoing \(1\)/)).toBeInTheDocument()
    expect(within(inspector).getByText("PRODUCES")).toBeInTheDocument()
    expect(within(inspector).getByText("Repo")).toBeInTheDocument()
    // The Connections field reflects that single degree.
    const connections = within(inspector).getByText("Connections").parentElement
    expect(connections?.textContent).toContain("1")
  })

  it("shows the all-unmapped banner whose 'Open Sources' calls onMapSource", async () => {
    const { onMapSource } = renderDialog(allUnmappedGraph)
    await screen.findByTestId("force-graph")

    expect(
      screen.getByText(/None of this workspace's sources are mapped yet/i),
    ).toBeInTheDocument()
    const openSources = screen.getByRole("button", { name: /Open Sources/i })
    fireEvent.click(openSources)
    expect(onMapSource).toHaveBeenCalledTimes(1)
  })
})
