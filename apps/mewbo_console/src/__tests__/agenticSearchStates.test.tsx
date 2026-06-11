/**
 * Rendering tests for the Agentic Search run lifecycle edge states.
 * The panels render exclusively from reducer-shaped `RunPayload` state —
 * these pin the failed / cancelled / empty-results surfaces.
 */

import { afterEach, describe, expect, it } from "vitest"
import { cleanup, render, screen } from "@testing-library/react"

afterEach(cleanup)

import { ResultsPanel } from "../components/agentic_search/ResultsPanel"
import type {
  RunPayload,
  SourceCatalogEntry,
  Workspace,
} from "../types/agenticSearch"

const workspace: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  created_at: "2026-05-01T00:00:00Z",
  past_queries: [],
}

const sources: SourceCatalogEntry[] = [
  { id: "github", name: "GitHub", color: "#fff", bg: "#000", glyph: "G", desc: "Code" },
]

function makeRun(overrides: Partial<RunPayload> = {}): RunPayload {
  return {
    run_id: "r1",
    query: "where is auth",
    workspace_id: "w1",
    status: "completed",
    total_ms: 4200,
    answer: { tldr: "", bullets: [], confidence: 0, sources_count: 0 },
    results: [],
    trace: [],
    related_questions: [],
    related_people: [],
    error: null,
    ...overrides,
  }
}

function renderPanel(run: RunPayload) {
  return render(
    <ResultsPanel
      workspace={workspace}
      workspaces={[workspace]}
      sources={sources}
      query={run.query}
      run={run}
      elapsedMs={run.total_ms}
      done
      answerReady={false}
      isLoading={false}
      tier="auto"
      onTierChange={() => undefined}
      onRun={() => undefined}
      onPickWorkspace={() => undefined}
      onOpenCreate={() => undefined}
      onOpenConfig={() => undefined}
    />,
  )
}

describe("ResultsPanel terminal states", () => {
  it("renders run.error as a destructive alert when the run failed", () => {
    renderPanel(makeRun({ status: "failed", error: "fan-out crashed" }))
    const alert = screen.getByRole("alert")
    expect(alert).toHaveTextContent("Search failed")
    expect(alert).toHaveTextContent("fan-out crashed")
    // No forever-pulsing synthesis skeleton for an answerless terminal run.
    expect(screen.queryByText("Synthesis")).not.toBeInTheDocument()
  })

  it("renders a calm cancelled state for cancelled runs", () => {
    renderPanel(makeRun({ status: "cancelled" }))
    expect(screen.getByText(/Search was cancelled/)).toBeInTheDocument()
    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
  })

  it("renders a deliberate empty state for a completed run with zero results", () => {
    renderPanel(makeRun({ status: "completed" }))
    expect(screen.getByText("No results")).toBeInTheDocument()
    expect(screen.getByText("Refine query")).toBeInTheDocument()
  })
})
