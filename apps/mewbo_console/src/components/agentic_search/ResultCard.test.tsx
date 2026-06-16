/**
 * ResultCard — the interaction-model + rich-card contract.
 *
 * The original card was a UI anti-pattern: clicking the title EXPANDED the card
 * (instead of navigating), url-less cards rendered a dead `https://` "Open"
 * link, author/timestamp rendered empty slots with dangling separators, and the
 * Sparkles button was a literal no-op. These pin the rebuilt contract.
 *
 * vitest runs WITHOUT globals → explicit cleanup (console convention).
 */
import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react"

import { ResultCard } from "./ResultCard"
import type { SearchResult, SourceCatalogEntry } from "../../types/agenticSearch"

afterEach(cleanup)

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

function renderCard(over: Partial<SearchResult> = {}, props: Partial<Parameters<typeof ResultCard>[0]> = {}) {
  return render(
    <ResultCard
      result={makeResult(over)}
      num={1}
      expanded={false}
      highlighted={false}
      sources={sources}
      onToggle={vi.fn()}
      {...props}
    />
  )
}

describe("ResultCard — title is a link, not a toggle", () => {
  it("renders the title as an <a href> to the target (new tab, rel noopener)", () => {
    renderCard()
    const link = screen.getByRole("link", { name: "auth handler" })
    expect(link).toHaveAttribute("href", "https://example.com/auth")
    expect(link).toHaveAttribute("target", "_blank")
    expect(link.getAttribute("rel")).toContain("noopener")
  })

  it("clicking the title does NOT toggle the card (no whole-card onClick)", () => {
    const onToggle = vi.fn()
    renderCard({}, { onToggle })
    fireEvent.click(screen.getByRole("link", { name: "auth handler" }))
    expect(onToggle).not.toHaveBeenCalled()
  })
})

describe("ResultCard — url-less cards have no dead Open/copy", () => {
  it("renders the title as plain text and no external-link / copy actions", () => {
    renderCard({ url: "" })
    // Title is not a link…
    expect(screen.queryByRole("link", { name: "auth handler" })).not.toBeInTheDocument()
    expect(screen.getByText("auth handler")).toBeInTheDocument()
    // …and there is no "Open in new tab" / "Copy link" action.
    expect(screen.queryByLabelText("Open in new tab")).not.toBeInTheDocument()
    expect(screen.queryByText("Copy link")).not.toBeInTheDocument()
  })
})

describe("ResultCard — empty author/timestamp render nothing (no dangling ·)", () => {
  it("omits author and timestamp when blank", () => {
    const { container } = renderCard({ author: "", timestamp: "" })
    expect(screen.queryByText("octocat")).not.toBeInTheDocument()
    // The footer separator dots used to dangle around empty slots.
    expect(container.textContent).not.toContain("·")
  })
})

describe("ResultCard — structured meta chips", () => {
  it("renders compact count chips and a labelled tag chip", () => {
    renderCard({ meta: { stars: 46_200, language: "TypeScript" } })
    expect(screen.getByText("46.2k")).toBeInTheDocument()
    expect(screen.getByText("TypeScript")).toBeInTheDocument()
  })

  it("shows ~6 chips with a +N overflow when there are more", () => {
    renderCard({
      meta: { a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7, h: 8 },
    })
    // 8 keys → 6 visible + "+2" overflow indicator at rest.
    expect(screen.getByText("+2")).toBeInTheDocument()
  })
})

describe("ResultCard — expand affordance only when expandable", () => {
  it("hides More/Less for a plain card with nothing to expand", () => {
    renderCard({ meta: null, snippet: "short", refs: [], insight: null })
    expect(screen.queryByRole("button", { name: /More|Less/ })).not.toBeInTheDocument()
  })

  it("shows More when there's overflow meta to reveal", () => {
    renderCard({ meta: { a: 1, b: 2, c: 3, d: 4, e: 5, f: 6, g: 7 } })
    expect(screen.getByRole("button", { name: /More/ })).toBeInTheDocument()
  })
})

describe("ResultCard — follow-up wiring", () => {
  it("the Sparkles button invokes onAskFollowUp with the result", () => {
    const onAskFollowUp = vi.fn()
    renderCard({}, { onAskFollowUp })
    const btn = screen.getByLabelText("Ask a follow-up about this result")
    fireEvent.click(btn)
    expect(onAskFollowUp).toHaveBeenCalledTimes(1)
    expect(onAskFollowUp.mock.calls[0][0].id).toBe("res-1")
  })

  it("renders no Sparkles button when no handler is wired (no dead control)", () => {
    renderCard()
    expect(screen.queryByLabelText("Ask a follow-up about this result")).not.toBeInTheDocument()
  })
})

describe("ResultCard — kind badge + accessible relevance", () => {
  it("shows the kind on the card and an aria-labelled relevance chip", () => {
    const { container } = renderCard({ kind: "code", relevance: 0.92 })
    expect(within(container).getByText("Code")).toBeInTheDocument()
    expect(screen.getByLabelText(/High relevance \(92%\)/)).toBeInTheDocument()
  })
})
