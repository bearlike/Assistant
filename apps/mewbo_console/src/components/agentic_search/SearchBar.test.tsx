/**
 * SearchBar scope-control tests.
 *
 * Tier + model + sources collapse into ONE progressively-disclosed pill (the
 * `SearchScopeControl`, a `DropdownMenu`) so the composer stays calm. The pill
 * still describes the SAME backend resolution — `run.model or the tier's
 * preset` — so the UI keeps it legible:
 *  1. The pill's resting label NAMES the resolved run config
 *     ("Auto · claude-sonnet-4-6") once `GET /tiers` resolves.
 *  2. An explicit override replaces the preset in that label.
 *  3. Opening the menu reveals the budget rows (depth · fan-out hints + the
 *     per-tier model presets), a Model sub-menu, and a Sources row — picking a
 *     tier emits it.
 *
 * vitest runs WITHOUT globals, so cleanup is wired explicitly (console
 * convention). useTiers/useModels are mocked at the hook seam — the only I/O
 * boundary — everything else renders the real component tree.
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { SearchBar } from "./SearchBar"
import type { Workspace } from "../../types/agenticSearch"

vi.mock("../../hooks/useAgenticSearch", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../../hooks/useAgenticSearch")>()
  return {
    ...mod,
    useTiers: () => ({
      data: {
        default_tier: "auto",
        tiers: {
          fast: "openai/gpt-5.4-nano",
          auto: "anthropic/claude-sonnet-4-6",
          deep: "openai/gpt-5.5",
        },
      },
    }),
  }
})

vi.mock("../../hooks/useModels", () => ({
  useModels: () => ({
    models: ["openai/gpt-5.5", "anthropic/claude-sonnet-4-6"],
    defaultModel: "",
    capabilities: {},
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}))

afterEach(cleanup)

const SCOPE_PILL_LABEL = "Search scope"

function workspace(): Workspace {
  return {
    id: "w1",
    name: "Platform",
    desc: "Infra and CI",
    sources: ["github"],
    instructions: "",
    created: "today",
    past_queries: [],
  }
}

function renderBar(over: Partial<Parameters<typeof SearchBar>[0]> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <SearchBar
        value=""
        onChange={vi.fn()}
        onSubmit={vi.fn()}
        workspace={workspace()}
        workspaces={[workspace()]}
        onPickWorkspace={vi.fn()}
        onNewWorkspace={vi.fn()}
        tier="auto"
        onTierChange={vi.fn()}
        model=""
        onModelChange={vi.fn()}
        {...over}
      />
    </QueryClientProvider>,
  )
}

describe("SearchBar scope control (tier · model · sources)", () => {
  it("scope pill resting label names the resolved run config", () => {
    renderBar()
    const pill = screen.getByRole("button", { name: SCOPE_PILL_LABEL })
    expect(pill).toHaveTextContent("Auto · claude-sonnet-4-6")
  })

  it("resting label follows the tier prop (Deep names the deep preset)", () => {
    renderBar({ tier: "deep" })
    expect(screen.getByRole("button", { name: SCOPE_PILL_LABEL })).toHaveTextContent(
      "Deep · gpt-5.5",
    )
  })

  it("an explicit override replaces the preset in the label", () => {
    renderBar({ model: "openai/gpt-5.5" })
    const pill = screen.getByRole("button", { name: SCOPE_PILL_LABEL })
    expect(pill).toHaveTextContent("Auto · gpt-5.5")
  })

  it("the menu shows budget rows + a model sub-menu + a sources row", () => {
    renderBar({ onOpenConfig: vi.fn() })
    // The scope pill is a Radix DropdownMenu trigger — it opens from keyboard
    // (the reliable jsdom path; no PointerEvent capture semantics needed).
    fireEvent.keyDown(screen.getByRole("button", { name: SCOPE_PILL_LABEL }), { key: "Enter" })
    expect(screen.getByText("Search budget — depth · fan-out · model")).toBeInTheDocument()
    // Budget hints speak depth × probes, not speed adjectives.
    expect(screen.getByText("shallow · few probes")).toBeInTheDocument()
    expect(screen.getByText("max depth · wide fan-out")).toBeInTheDocument()
    // Each tier row names the model preset it runs on.
    expect(screen.getByText("gpt-5.4-nano")).toBeInTheDocument()
    // The model override (sub-menu) and the sources jump are both present
    // (progressive disclosure — they're behind the one pill, not the toolbar).
    expect(screen.getByText("Model")).toBeInTheDocument()
    expect(screen.getByText("Sources")).toBeInTheDocument()
  })

  it("picking a tier row emits onTierChange with the tier id", () => {
    const onTierChange = vi.fn()
    renderBar({ onTierChange })
    fireEvent.keyDown(screen.getByRole("button", { name: SCOPE_PILL_LABEL }), { key: "Enter" })
    fireEvent.click(screen.getByText("max depth · wide fan-out"))
    expect(onTierChange).toHaveBeenCalledWith("deep")
  })
})

describe("SearchBar — past-query suggestions dedupe + unique identity (#98)", () => {
  // cmdk identifies CommandItems by `value`; identical query text under a
  // shared value made every rerun of a query hover/select as one. The fix:
  // dedupe the rendered Recent list by normalized text (keep most recent) AND
  // give each surviving item a unique value (run_id, else `<q>-<index>`).
  function wsWithHistory(past: Workspace["past_queries"]): Workspace {
    return { ...workspace(), past_queries: past }
  }

  function openSuggestions() {
    // The dropdown opens on a genuine focus gesture (mount-time autoFocus is
    // suppressed, but this test never sets autoFocus). cmdk needs the focus.
    const input = screen.getByRole<HTMLInputElement>("combobox")
    fireEvent.focus(input)
    return input
  }

  it("collapses three identical past queries into ONE selectable item", () => {
    const ws = wsWithHistory([
      { q: "where is auth", when: "1m", results: 3, run_id: "run-c" },
      { q: "where is auth", when: "1h", results: 3, run_id: "run-b" },
      { q: "where is auth", when: "2h", results: 3, run_id: "run-a" },
    ])
    renderBar({ workspace: ws, workspaces: [ws] })
    openSuggestions()
    // Exactly one rendered Recent row carries the query text.
    expect(screen.getAllByText("where is auth")).toHaveLength(1)
  })

  it("keeps the MOST RECENT duplicate (first occurrence, backend prepends)", () => {
    const onReplay = vi.fn()
    const ws = wsWithHistory([
      { q: "deploy flow", when: "now", results: 5, run_id: "run-newest" },
      { q: "deploy flow", when: "old", results: 5, run_id: "run-oldest" },
    ])
    renderBar({ workspace: ws, workspaces: [ws], onReplay })
    openSuggestions()
    fireEvent.click(screen.getByText("deploy flow"))
    // The surviving row is the first (most recent) entry's run.
    expect(onReplay).toHaveBeenCalledWith("run-newest")
  })

  it("renders two DIFFERENT queries as two independently selectable items", () => {
    const onReplay = vi.fn()
    const ws = wsWithHistory([
      { q: "where is auth", when: "1m", results: 3, run_id: "run-1" },
      { q: "build pipeline", when: "2m", results: 1, run_id: "run-2" },
    ])
    renderBar({ workspace: ws, workspaces: [ws], onReplay })
    openSuggestions()
    expect(screen.getByText("where is auth")).toBeInTheDocument()
    expect(screen.getByText("build pipeline")).toBeInTheDocument()
    // Each selects its own run — distinct values mean distinct identity.
    fireEvent.click(screen.getByText("build pipeline"))
    expect(onReplay).toHaveBeenCalledWith("run-2")
  })
})

describe("SearchBar — the input is the single query home (#96)", () => {
  // The results band echoed the query as italic subtext; the fix moves the
  // query to exactly ONE place — the bar's input. The compact bar reflects the
  // controlled `value` and surfaces no second copy of the query string.
  it("compact bar reflects the controlled value in the input only", () => {
    renderBar({ value: "where is auth", variant: "compact" })
    const input = screen.getByRole<HTMLInputElement>("combobox")
    expect(input.value).toBe("where is auth")
    // No quoted echo of the query anywhere in the bar.
    expect(screen.queryByText('"where is auth"')).not.toBeInTheDocument()
  })
})
