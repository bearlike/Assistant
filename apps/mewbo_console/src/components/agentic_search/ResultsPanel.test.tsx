/**
 * ResultsPanel top-band tests (#96).
 *
 * The results page top section was "crap": the query repeated inside the input
 * AND as an italic subtext echo, and the stats line always read
 * "0 results · 0.0 seconds · complete". These pin the rebuilt band's contract:
 *
 *  1. The query appears ONCE — in the SearchBar input (editing is the input's
 *     job) — never a second italic-mono echo.
 *  2. A finished run with a real `total_ms` shows real seconds and NEVER "0.0s".
 *  3. A finished run with an UNKNOWN duration (elapsedMs 0) omits the seconds
 *     entirely — silence, not a fabricated "0.0s".
 *  4. A zero-results finished run SUPPRESSES the "0 results" count in the band
 *     (the results column owns the honest empty state).
 *  5. A coordinator trace lane (source_id "") renders gracefully — its name,
 *     not a misleading per-source "0" count chip.
 *
 * vitest runs WITHOUT globals, so cleanup is wired explicitly (console
 * convention). The SearchBar's model/tier pills fetch via TanStack Query — mount
 * under a provider (retries off) like the app does; no network is hit.
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { ResultsPanel } from "./ResultsPanel"
import type {
  RunPayload,
  SearchResult,
  SourceCatalogEntry,
  TraceAgent,
  Workspace,
} from "../../types/agenticSearch"

afterEach(cleanup)

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

function makeResult(over: Partial<SearchResult> = {}): SearchResult {
  return {
    id: "res-1",
    source: "github",
    kind: "code",
    relevance: 0.9,
    title: "auth handler",
    url: "https://example.com/auth",
    snippet: "the auth path",
    author: "octocat",
    timestamp: "2026-05-01",
    ...over,
  }
}

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

function renderPanel(
  run: RunPayload,
  over: {
    elapsedMs?: number
    done?: boolean
    tier?: "fast" | "auto" | "deep"
    onDeeper?: ReturnType<typeof vi.fn>
    onCancel?: ReturnType<typeof vi.fn>
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <ResultsPanel
        workspace={workspace}
        workspaces={[workspace]}
        sources={sources}
        query={run.query}
        run={run}
        elapsedMs={over.elapsedMs ?? run.total_ms}
        done={over.done ?? true}
        answerReady={false}
        isLoading={false}
        tier={over.tier ?? "auto"}
        onTierChange={vi.fn()}
        model=""
        onModelChange={vi.fn()}
        onRun={vi.fn()}
        onDeeper={over.onDeeper}
        onCancel={over.onCancel}
        onPickWorkspace={vi.fn()}
        onOpenCreate={vi.fn()}
        onOpenConfig={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

describe("ResultsPanel top band — query echo", () => {
  it("shows the query ONCE (in the input), with no italic-mono subtext echo", () => {
    renderPanel(makeRun({ query: "where is auth" }))
    // The query lives in the SearchBar input as its value…
    const input = screen.getByRole<HTMLInputElement>("combobox")
    expect(input.value).toBe("where is auth")
    // …and nowhere else as quoted echo text. The old band rendered "{query}"
    // as an italic-mono span; that must be gone.
    expect(screen.queryByText('"where is auth"')).not.toBeInTheDocument()
  })
})

describe("ResultsPanel top band — honest stats line", () => {
  it("a finished run with real total_ms shows real seconds, never 0.0s", () => {
    renderPanel(makeRun({ total_ms: 4200 }))
    expect(screen.getByText(/4\.2s/)).toBeInTheDocument()
    expect(screen.getByText(/complete/)).toBeInTheDocument()
    // The headline regression: a finished run must NEVER read "0.0s".
    expect(screen.queryByText(/0\.0s/)).not.toBeInTheDocument()
  })

  it("a finished run with unknown duration omits the seconds entirely", () => {
    // No live elapsed and no real total_ms (snapshot with no usable timestamps).
    renderPanel(makeRun({ total_ms: 0 }), { elapsedMs: 0 })
    expect(screen.queryByText(/0\.0s/)).not.toBeInTheDocument()
    // Status still renders — duration is just silent, not fabricated.
    expect(screen.getByText(/complete/)).toBeInTheDocument()
  })

  it("a zero-results finished run hides the count in the band", () => {
    renderPanel(makeRun({ results: [], total_ms: 4200 }))
    // The band must NOT say "0 results" — the results column owns the empty
    // state ("No results" card below).
    expect(screen.queryByText(/\b0 results\b/)).not.toBeInTheDocument()
    expect(screen.getByText("No results")).toBeInTheDocument()
  })

  it("a finished run with results shows the count in the band", () => {
    renderPanel(
      makeRun({
        results: [makeResult({ id: "res-1" }), makeResult({ id: "res-2" })],
        total_ms: 3100,
      }),
    )
    expect(screen.getByText(/2 results/)).toBeInTheDocument()
    expect(screen.getByText(/3\.1s/)).toBeInTheDocument()
  })

  it("a streaming run shows 'streaming · Ns', not a completion line", () => {
    renderPanel(makeRun({ status: "running", total_ms: 0 }), {
      elapsedMs: 2400,
      done: false,
    })
    expect(screen.getByText("streaming")).toBeInTheDocument()
    expect(screen.queryByText(/complete/)).not.toBeInTheDocument()
  })
})

describe("ResultsPanel — coordinator trace lane", () => {
  const coordinatorAgent: TraceAgent = {
    id: "coord",
    agent_id: "coord",
    name: "scg-search",
    source_id: "", // the root agent's tool activity — no catalog source
    slot: 0,
    lines: [{ glyph: "✓", text: "1 results", done: true }],
  }

  it("renders the coordinator lane by name with no misleading per-source 0", () => {
    // Mid-run so the ProgressStrip mounts; the coordinator finished (done line).
    renderPanel(
      makeRun({ status: "running", trace: [coordinatorAgent], total_ms: 0 }),
      { elapsedMs: 1000, done: false },
    )
    // The lane shows its honest name (the strip + the right rail each list it;
    // both must render it gracefully, never an empty pill).
    const labels = screen.getAllByText("scg-search")
    expect(labels.length).toBeGreaterThan(0)
    // The ProgressStrip pill is the rounded-full state container; it must NOT
    // render a per-source result count chip (a misleading "0").
    const pill = labels
      .map((el) => el.closest("div"))
      .find((d) => d?.className.includes("rounded-full"))
    expect(pill).not.toBeUndefined()
    expect(within(pill as HTMLElement).queryByText("0")).not.toBeInTheDocument()
  })
})

describe("ResultCard — agent-emitted per-card confidence (#102)", () => {
  it("renders the emitter's confidence % when present, and never a fake 0%", () => {
    renderPanel(
      makeRun({
        results: [
          makeResult({ id: "res-1", confidence: 0.7 }),
          makeResult({ id: "res-2", title: "no confidence" }),
        ],
        total_ms: 3100,
      }),
    )
    // The probe-emitted card shows its confidence beside the relevance dot…
    const chip = screen.getByLabelText("Agent confidence 70%")
    expect(chip).toHaveTextContent("70%")
    // …and a card without one renders no confidence chip at all (honest
    // absence — the relevance dot alone, never an invented 0%).
    expect(screen.getAllByLabelText(/Agent confidence/)).toHaveLength(1)
  })
})

describe("ResultsPanel — filter rail hides zero-count kinds", () => {
  it("renders only the kinds that have results (plus implicit All)", () => {
    renderPanel(
      makeRun({
        results: [
          makeResult({ id: "r1", kind: "code" }),
          makeResult({ id: "r2", kind: "docs" }),
        ],
        total_ms: 3100,
      }),
    )
    // Code + Docs are populated → rendered as filter chips alongside "All".
    expect(screen.getByRole("button", { name: /Code/ })).toBeInTheDocument()
    expect(screen.getByRole("button", { name: /Docs/ })).toBeInTheDocument()
    // Threads/Design/Tickets/Web have zero results → NOT rendered at all
    // (the old greyed-out chip looked identical to a populated one).
    expect(screen.queryByRole("button", { name: /Threads/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Tickets/ })).not.toBeInTheDocument()
    expect(screen.queryByRole("button", { name: /Web/ })).not.toBeInTheDocument()
  })
})

describe("ResultsPanel — Go deeper escalates the tier", () => {
  it("re-runs the query at the next tier up (auto → deep)", () => {
    const onDeeper = vi.fn()
    renderPanel(
      makeRun({
        tier: "auto",
        results: [makeResult({ id: "r1" })],
        total_ms: 3100,
      }),
      { onDeeper },
    )
    const btn = screen.getByRole("button", { name: /Go deeper \(deep\)/ })
    fireEvent.click(btn)
    expect(onDeeper).toHaveBeenCalledWith("where is auth", "deep")
  })

  it("hides Go deeper at the top of the ladder (deep)", () => {
    const onDeeper = vi.fn()
    renderPanel(
      makeRun({
        tier: "deep",
        results: [makeResult({ id: "r1" })],
        total_ms: 3100,
      }),
      { onDeeper },
    )
    expect(screen.queryByRole("button", { name: /Go deeper/ })).not.toBeInTheDocument()
  })
})

describe("ResultsPanel — per-card follow-up prefills the composer", () => {
  it("clicking a card's Sparkles prefills the input with the result context", () => {
    renderPanel(
      makeRun({
        results: [makeResult({ id: "r1", title: "auth handler", url: "https://x/auth" })],
        total_ms: 3100,
      }),
    )
    const input = screen.getByRole<HTMLInputElement>("combobox")
    expect(input.value).toBe("where is auth")
    fireEvent.click(screen.getByLabelText("Ask a follow-up about this result"))
    // The composer is prefilled with a context line referencing the result.
    expect(input.value).toContain("auth handler")
    expect(input.value).toContain("https://x/auth")
  })
})

describe("ResultsPanel — Cancel while running", () => {
  it("shows a Cancel control mid-run that fires onCancel", () => {
    const onCancel = vi.fn()
    renderPanel(makeRun({ status: "running", total_ms: 0 }), {
      elapsedMs: 1200,
      done: false,
      onCancel,
    })
    const btn = screen.getByRole("button", { name: /Cancel/ })
    fireEvent.click(btn)
    expect(onCancel).toHaveBeenCalledTimes(1)
  })
})
