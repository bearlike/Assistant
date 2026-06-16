/**
 * RightRail — trace instrument panel.
 *
 * Per-lane rows lead with the lane KIND (its role), not the model name (the
 * model is a separate metric). The overall run-stats block renders only the
 * fields the BE actually stamped (honesty rule — never a fabricated 0).
 *
 * vitest runs WITHOUT globals → explicit cleanup (console convention).
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"

import { RightRail } from "./RightRail"
import type {
  RunStats,
  SourceCatalogEntry,
  TraceAgent,
} from "../../types/agenticSearch"

afterEach(cleanup)

const sources: SourceCatalogEntry[] = [
  { id: "github", name: "GitHub", color: "#fff", bg: "#000", glyph: "G", desc: "Code" },
]

function lane(over: Partial<TraceAgent> = {}): TraceAgent {
  return {
    id: "a1",
    agent_id: "a1",
    name: "scg-path-probe",
    source_id: "github",
    slot: 1,
    lines: [{ glyph: "✓", text: "2 results", done: true }],
    ...over,
  }
}

function renderRail(
  agents: TraceAgent[],
  over: { stats?: RunStats | null } = {}
) {
  return render(
    <RightRail
      agents={agents}
      sources={sources}
      stats={over.stats ?? null}
      related={[]}
      people={[]}
      done
      traceActive={false}
      onShowTrace={vi.fn()}
      onAsk={vi.fn()}
    />
  )
}

describe("RightRail — lane rows lead with kind, not model", () => {
  it("shows the lane kind as the headline and the model in the metric strip", () => {
    renderRail([
      lane({ kind: "scg-path-probe", model: "claude-sonnet-4-6", steps: 4, results_count: 2 }),
    ])
    // The lane's KIND is the row headline…
    expect(screen.getByText("scg-path-probe")).toBeInTheDocument()
    // …and the model rides the metric strip beside steps/results.
    expect(screen.getByText(/claude-sonnet-4-6/)).toBeInTheDocument()
    expect(screen.getByText(/4 steps/)).toBeInTheDocument()
  })

  it("renders metrics only when present (no fabricated zeros)", () => {
    renderRail([lane({ kind: "coordinator", source_id: "", model: null })])
    expect(screen.getByText("coordinator")).toBeInTheDocument()
    // No model / steps / token metrics were stamped → no metric strip text.
    expect(screen.queryByText(/step/)).not.toBeInTheDocument()
    expect(screen.queryByText(/tok/)).not.toBeInTheDocument()
  })
})

describe("RightRail — per-lane result counts (returned vs filtered)", () => {
  it("surfaces the kept count as a pip and the filtered delta in the strip", () => {
    renderRail([
      lane({ kind: "scg-path-probe", results_count: 2, returned_count: 3 }),
    ])
    // The kept count rides a prominent pip (the headline contribution)…
    const pip = screen.getByTitle(/2 results · 1 filtered as duplicate/)
    expect(pip).toHaveTextContent("2")
    // …and the deduped delta is legible in the metric strip.
    expect(screen.getByText(/1 filtered/)).toBeInTheDocument()
  })

  it("shows no filtered clause when nothing was deduped", () => {
    renderRail([lane({ kind: "scg-path-probe", results_count: 2, returned_count: 2 })])
    expect(screen.getByTitle(/^2 results$/)).toBeInTheDocument()
    expect(screen.queryByText(/filtered/)).not.toBeInTheDocument()
  })

  it("hides the count pip for the coordinator lane (no per-source count)", () => {
    renderRail([
      lane({ kind: "coordinator", source_id: "", results_count: 0, returned_count: 1 }),
    ])
    // The coordinator never shows a misleading per-source count pip.
    expect(screen.queryByText(/filtered/)).not.toBeInTheDocument()
  })
})

describe("RightRail — run-stats block", () => {
  it("renders nothing when stats is absent", () => {
    // Use a lane whose kind won't false-match the stats regex (the lane name
    // "scg-path-probe" contains "probe"); a coordinator lane is clean.
    renderRail([lane({ kind: "coordinator", source_id: "", name: "coordinator" })])
    expect(screen.queryByText(/\d+ probes?/)).not.toBeInTheDocument()
    expect(screen.queryByText(/tool call/)).not.toBeInTheDocument()
  })

  it("renders only the present fields, with the setup/search split", () => {
    renderRail([lane()], {
      stats: {
        probes: 3,
        tool_calls: 7,
        input_tokens: 12_400,
        output_tokens: 900,
        setup_ms: 32_000,
        search_ms: 41_000,
      },
    })
    expect(screen.getByText(/3 probes/)).toBeInTheDocument()
    expect(screen.getByText(/7 tool calls/)).toBeInTheDocument()
    expect(screen.getByText(/setup 32\.0s/)).toBeInTheDocument()
    expect(screen.getByText(/search 41\.0s/)).toBeInTheDocument()
    expect(screen.getByText(/12\.4k→900 tok/)).toBeInTheDocument()
  })

  it("omits the phase split when neither setup nor search is known", () => {
    renderRail([lane()], {
      stats: {
        probes: 2,
        tool_calls: 0,
        input_tokens: 0,
        output_tokens: 0,
        setup_ms: null,
        search_ms: null,
      },
    })
    expect(screen.getByText(/2 probes/)).toBeInTheDocument()
    // No "0 tool calls", no "setup …", no "tok" line.
    expect(screen.queryByText(/tool call/)).not.toBeInTheDocument()
    expect(screen.queryByText(/setup/)).not.toBeInTheDocument()
    expect(screen.queryByText(/tok/)).not.toBeInTheDocument()
  })
})
