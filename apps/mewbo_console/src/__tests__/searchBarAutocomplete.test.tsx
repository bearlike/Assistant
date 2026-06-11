/**
 * SearchBar autocomplete-open behaviour (#82).
 *
 * The suggestions dropdown must be CLOSED on landing-page load — even though
 * the hero bar auto-focuses its input — and open only on a genuine focus/typing
 * gesture, closing on Escape. The dropdown is gated on `acOpen`; we assert via
 * the presence of the "Recent in <workspace>" / "Switch workspace" group
 * headings which only render inside the open dropdown.
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"

afterEach(cleanup)

import { SearchBar } from "../components/agentic_search/SearchBar"
import type { Workspace } from "../types/agenticSearch"

const wsA: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  past_queries: [
    { q: "where is auth", when: "yesterday", results: 3, run_id: "run-1" },
  ],
}
const wsB: Workspace = {
  id: "w2",
  name: "Design",
  desc: "Figma",
  sources: [],
  instructions: "",
  created: "May 2026",
  past_queries: [],
}

function renderBar(over: Partial<React.ComponentProps<typeof SearchBar>> = {}) {
  return render(
    <SearchBar
      value=""
      onChange={vi.fn()}
      onSubmit={vi.fn()}
      workspace={wsA}
      workspaces={[wsA, wsB]}
      onPickWorkspace={vi.fn()}
      onNewWorkspace={vi.fn()}
      variant="hero"
      autoFocus
      {...over}
    />,
  )
}

describe("SearchBar autocomplete default-open bug", () => {
  it("does NOT open the suggestions on mount despite autoFocus", () => {
    renderBar()
    // The dropdown's group headings are only in the DOM when it is open.
    expect(screen.queryByText(/Recent in Eng/i)).toBeNull()
    expect(screen.queryByText(/Switch workspace/i)).toBeNull()
  })

  it("opens the suggestions on a user focus gesture", () => {
    renderBar()
    const input = screen.getByPlaceholderText("Ask or search the workspace…")
    // A genuine (non-mount) focus — blur first so the next focus is user-driven.
    fireEvent.blur(input)
    fireEvent.focus(input)
    expect(screen.getByText(/Recent in Eng/i)).toBeInTheDocument()
  })

  it("closes the suggestions on Escape", () => {
    renderBar()
    const input = screen.getByPlaceholderText("Ask or search the workspace…")
    fireEvent.blur(input)
    fireEvent.focus(input)
    expect(screen.getByText(/Recent in Eng/i)).toBeInTheDocument()
    fireEvent.keyDown(input, { key: "Escape" })
    expect(screen.queryByText(/Recent in Eng/i)).toBeNull()
  })

  it("pans the open dropdown out from under the composer (entrance class)", () => {
    const { container } = renderBar()
    const input = screen.getByPlaceholderText("Ask or search the workspace…")
    fireEvent.blur(input)
    fireEvent.focus(input)
    // The dropdown anchors to the composer surface and animates from its top
    // edge (origin-top scale-y + fade) — the `.composer-suggest` class drives
    // it, and `prefers-reduced-motion` neutralises it in index.css.
    expect(container.querySelector(".composer-suggest")).not.toBeNull()
  })
})

describe("SearchBar hero composer — expand affordance removed (#82)", () => {
  it("renders no Expand button in the hero bar", () => {
    renderBar()
    // The Maximize2 expand button was wired to nothing; it was removed rather
    // than left as a dead control. Voice/Attach/Search remain.
    expect(screen.queryByRole("button", { name: /expand/i })).toBeNull()
    expect(screen.getByRole("button", { name: /^search$/i })).toBeInTheDocument()
  })
})

describe("SearchBar suggestions — replay-not-rerun is preserved", () => {
  it("replays a stored run by id instead of submitting a fresh query", () => {
    const onReplay = vi.fn()
    const onSubmit = vi.fn()
    renderBar({ onReplay, onSubmit })
    const input = screen.getByPlaceholderText("Ask or search the workspace…")
    fireEvent.blur(input)
    fireEvent.focus(input)
    // The lone recent query (run_id "run-1") must REPLAY, never POST a new run.
    fireEvent.click(screen.getByText("where is auth"))
    expect(onReplay).toHaveBeenCalledWith("run-1")
    expect(onSubmit).not.toHaveBeenCalled()
  })
})
