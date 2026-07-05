/**
 * Direct tests for the shared 3D graph engine ``Graph3DView`` — the ONE renderer
 * the wiki Knowledge Graph and the Agentic-Search SCG graph both inject a theme
 * into. Domain-agnostic, so a tiny synthetic theme + graph exercises the toolbar
 * grammar, the per-kind visibility filter, and the inspector render-prop without
 * any wiki/SCG vocabulary.
 *
 * ``react-force-graph-3d`` (three.js/WebGL) is mocked into clickable node buttons
 * that honour the engine's ``nodeVisibility`` accessor, so hiding a kind in the
 * toolbar provably drops its nodes from what the canvas would draw.
 */
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import userEvent from "@testing-library/user-event"

import {
  Graph3DView,
  type Graph3DInspectorCtx,
  type Graph3DPick,
  type Graph3DStats,
  type Graph3DTheme,
  type Graph3DWire,
} from "@/components/wiki/Graph3DView"

afterEach(cleanup)

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

beforeAll(() => {
  Object.defineProperty(HTMLElement.prototype, "clientWidth", { configurable: true, get: () => 800 })
  Object.defineProperty(HTMLElement.prototype, "clientHeight", { configurable: true, get: () => 600 })
})

// ── A synthetic two-kind, one-layer domain ───────────────────────────────────
const theme: Graph3DTheme = {
  allKinds: ["Alpha", "Beta"],
  kindVar: { Alpha: "--graph-function", Beta: "--graph-class" },
  kindDot: { Alpha: "bg-[hsl(var(--graph-function))]", Beta: "bg-[hsl(var(--graph-class))]" },
  kindLabel: { Alpha: "Alpha", Beta: "Beta" },
  edgeVar: { LINK: "--graph-edge-soft" },
  kindLayer: { Alpha: "L1", Beta: "L1" },
  layerOrder: ["L1"],
  layerLabel: { L1: "Layer One" },
  layerDot: { L1: "bg-[hsl(var(--graph-file))]" },
}

const graph: Graph3DWire = {
  nodes: [
    { data: { id: "a1", label: "Alpha One", kind: "Alpha" } },
    { data: { id: "a2", label: "Alpha Two", kind: "Alpha" } },
    { data: { id: "b1", label: "Beta One", kind: "Beta" } },
  ],
  edges: [{ data: { id: "e1", source: "a1", target: "b1", kind: "LINK" } }],
}

const stats: Graph3DStats = { nodeCount: 3, edgeCount: 1, kinds: { Alpha: 2, Beta: 1 } }

function renderView(
  renderInspector?: (pick: Graph3DPick, ctx: Graph3DInspectorCtx) => React.ReactNode,
) {
  return render(
    <Graph3DView
      graph={graph}
      stats={stats}
      isPending={false}
      isError={false}
      theme={theme}
      renderInspector={renderInspector}
    />,
  )
}

describe("Graph3DView — shared engine", () => {
  it("renders the toolbar stats line from the injected stats", async () => {
    renderView()
    await screen.findByTestId("force-graph")
    const statsLine = screen.getByText((_content, el) => {
      const norm = el?.textContent?.replace(/\s+/g, " ").trim() ?? ""
      return /^\d+ nodes·\d+ edges$/.test(norm)
    })
    expect(statsLine.textContent?.replace(/\s+/g, " ").trim()).toBe("3 nodes·1 edges")
  })

  it("hides a kind's nodes from the canvas when toggled off in the Node types popover", async () => {
    const user = userEvent.setup()
    renderView()
    await screen.findByTestId("force-graph")
    // Both Alpha nodes start visible (the engine's nodeVisibility passes them).
    expect(screen.getByTestId("node-a1")).toBeInTheDocument()
    expect(screen.getByTestId("node-a2")).toBeInTheDocument()
    expect(screen.getByTestId("node-b1")).toBeInTheDocument()

    await user.click(screen.getByRole("button", { name: /Node types/i }))
    await user.click(screen.getByRole("button", { name: /Alpha/ }))

    // Alpha is now hidden → nodeVisibility(node) === false → dropped from graphData.
    expect(screen.queryByTestId("node-a1")).toBeNull()
    expect(screen.queryByTestId("node-a2")).toBeNull()
    // Untoggled kinds stay drawn.
    expect(screen.getByTestId("node-b1")).toBeInTheDocument()
  })

  it("invokes renderInspector with the clicked node as the pick", async () => {
    const renderInspector = vi.fn(
      (pick: Graph3DPick): React.ReactNode => (
        <div data-testid="inspector">{pick.node?.id}</div>
      ),
    )
    renderView(renderInspector)
    // No pick yet → the inspector slot stays empty.
    expect(screen.queryByTestId("inspector")).toBeNull()

    fireEvent.click(await screen.findByTestId("node-b1"))

    expect(screen.getByTestId("inspector")).toHaveTextContent("b1")
    const lastPick = renderInspector.mock.calls.at(-1)?.[0]
    expect(lastPick?.node?.id).toBe("b1")
    expect(lastPick?.link).toBeNull()
  })
})
