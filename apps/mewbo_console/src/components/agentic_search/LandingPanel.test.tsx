/**
 * LandingPanel production-polish tests.
 *
 * Covers the two affordances the polish pass reshaped:
 *  1. The "Your workspaces" anchor is a real scroll button (mirrors HomeView's
 *     "Recent sessions ⌄"), not a static aria-hidden label.
 *  2. The past-query example chips REPLAY a stored run (run_id → onOpenRun) and
 *     fall back to a fresh search (no run_id → onSubmit) — the replay-not-rerun
 *     contract, with the stable `title` strings the inert test (#80) keys off.
 *
 * vitest runs WITHOUT globals, so cleanup is wired explicitly (console
 * convention) and we stub scrollIntoView (jsdom omits it).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { LandingPanel } from "./LandingPanel"
import type { Workspace } from "../../types/agenticSearch"

afterEach(cleanup)

beforeEach(() => {
  // jsdom has no layout engine — the anchor calls scrollIntoView on a ref.
  Element.prototype.scrollIntoView = vi.fn()
})

function workspace(over: Partial<Workspace> = {}): Workspace {
  return {
    id: "w1",
    name: "Platform",
    desc: "Infra and CI",
    sources: ["github"],
    instructions: "",
    created: "today",
    past_queries: [],
    ...over,
  }
}

function renderLanding(
  ws: Workspace,
  overrides: {
    onOpenRun?: (id: string) => void
    onSubmit?: (q: string) => void
  } = {},
) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LandingPanel
        workspace={ws}
        workspaces={[ws]}
        sources={[]}
        tier="auto"
        onTierChange={vi.fn()}
        model=""
        onModelChange={vi.fn()}
        onPickWorkspace={vi.fn()}
        onSubmit={overrides.onSubmit ?? vi.fn()}
        onOpenCreate={vi.fn()}
        onOpenConfig={vi.fn()}
        onOpenSources={vi.fn()}
        onOpenRun={overrides.onOpenRun ?? vi.fn()}
        onOpenGraph={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

describe("LandingPanel — workspaces anchor", () => {
  it("is a real button that scrolls the grid into view", () => {
    const spy = vi.spyOn(Element.prototype, "scrollIntoView")
    renderLanding(workspace())

    const anchor = screen.getByRole("button", { name: /scroll to your workspaces/i })
    expect(anchor).toBeInTheDocument()
    // Mirrors HomeView's chevron affordance — same bounce keyframe class.
    expect(anchor.className).toContain("animate-scroll-bounce")

    fireEvent.click(anchor)
    expect(spy).toHaveBeenCalledWith({ behavior: "smooth", block: "start" })
  })
})

describe("LandingPanel — past-query chips (replay vs rerun)", () => {
  it("REPLAYS a chip with a run_id via onOpenRun, never a fresh onSubmit", () => {
    const onOpenRun = vi.fn()
    const onSubmit = vi.fn()
    renderLanding(
      workspace({
        past_queries: [
          { q: "where is auth", when: "1d", results: 3, run_id: "run-123" },
        ],
      }),
      { onOpenRun, onSubmit },
    )

    const chip = screen.getByTitle("Replay this search")
    fireEvent.click(chip)
    expect(onOpenRun).toHaveBeenCalledWith("run-123")
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("re-runs a legacy chip without a run_id via onSubmit", () => {
    const onOpenRun = vi.fn()
    const onSubmit = vi.fn()
    renderLanding(
      workspace({
        past_queries: [{ q: "build pipeline", when: "2d", results: 1 }],
      }),
      { onOpenRun, onSubmit },
    )

    const chip = screen.getByTitle("Search this again")
    fireEvent.click(chip)
    expect(onSubmit).toHaveBeenCalledWith("build pipeline")
    expect(onOpenRun).not.toHaveBeenCalled()
  })

  it("caps each chip with truncate so a long query can't blow out the row", () => {
    renderLanding(
      workspace({
        past_queries: [
          {
            q: "an extremely long historical query that would otherwise span the entire hero width",
            when: "3d",
            results: 9,
            run_id: "run-long",
          },
        ],
      }),
    )
    const chip = screen.getByTitle("Replay this search")
    // Uniform sizing contract: bounded width + truncation + fixed height.
    expect(chip.className).toContain("max-w-[240px]")
    expect(chip.className).toContain("h-7")
    expect(chip.querySelector(".truncate")).not.toBeNull()
  })

  it("dedupes identical past queries into ONE example chip (#98)", () => {
    // Three runs of the same query collapse to a single chip — they previously
    // shared a `key={e.q}` and hovered/replayed as twins.
    renderLanding(
      workspace({
        past_queries: [
          { q: "where is auth", when: "1m", results: 3, run_id: "run-c" },
          { q: "where is auth", when: "1h", results: 3, run_id: "run-b" },
          { q: "where is auth", when: "2h", results: 3, run_id: "run-a" },
        ],
      }),
    )
    expect(screen.getAllByText("where is auth")).toHaveLength(1)
    expect(screen.getAllByTitle("Replay this search")).toHaveLength(1)
  })

  it("keeps the most recent duplicate's run for the surviving chip (#98)", () => {
    const onOpenRun = vi.fn()
    renderLanding(
      workspace({
        past_queries: [
          { q: "deploy flow", when: "now", results: 5, run_id: "run-newest" },
          { q: "deploy flow", when: "old", results: 5, run_id: "run-oldest" },
        ],
      }),
      { onOpenRun },
    )
    fireEvent.click(screen.getByTitle("Replay this search"))
    expect(onOpenRun).toHaveBeenCalledWith("run-newest")
  })
})

describe("LandingPanel — workspace card meta row is single-line (#98)", () => {
  // The meta/action shelf must NEVER wrap to a second line: `flex-nowrap` on
  // the row + `flex-none` on the action cluster + `whitespace-nowrap` on the
  // "N past" pill, pinned to the card bottom via `mt-auto`.
  it("pins the meta row to the card bottom and forbids wrapping", () => {
    renderLanding(workspace())
    const pastPill = screen.getByRole("button", { name: /recent runs in platform/i })
    // The "N past" pill is the load-bearing single-line guarantee.
    expect(pastPill.className).toContain("whitespace-nowrap")
    expect(pastPill.className).toContain("flex-none")

    // Its meta-row ancestor pins to the bottom and never wraps.
    const metaRow = pastPill.closest(".mt-auto")
    expect(metaRow).not.toBeNull()
    expect(metaRow?.className).toContain("flex-nowrap")
  })
})
