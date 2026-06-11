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
})
