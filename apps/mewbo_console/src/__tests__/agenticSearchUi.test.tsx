/**
 * Component tests for the Agentic Search surface: the LandingPanel's
 * client-side workspace filter (the FE counterpart of the server's `?q=`)
 * and ResultCard snippet rendering (tokens parsed, no raw HTML injection).
 */

import { afterEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

import { LandingPanel } from "../components/agentic_search/LandingPanel"
import { ResultCard } from "../components/agentic_search/ResultCard"
import type { SearchResult, Workspace } from "../types/agenticSearch"

afterEach(cleanup)

// ── Fixtures ─────────────────────────────────────────────────────────────────

function workspace(over: Partial<Workspace>): Workspace {
  return {
    id: "w1",
    name: "Platform",
    desc: "Infra and CI",
    sources: [],
    instructions: "",
    created: "today",
    past_queries: [],
    ...over,
  }
}

function renderLanding(
  workspaces: Workspace[],
  overrides: { onOpenConfig?: (w: Workspace) => void } = {},
) {
  // The workspace cards' run-history chip uses TanStack Query (lazily) — the
  // panel must mount under a QueryClientProvider like in the app.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <LandingPanel
        workspace={workspaces[0]}
        workspaces={workspaces}
        sources={[]}
        tier="auto"
        onTierChange={vi.fn()}
        onPickWorkspace={vi.fn()}
        onSubmit={vi.fn()}
        onOpenCreate={vi.fn()}
        onOpenConfig={overrides.onOpenConfig ?? vi.fn()}
        onOpenSources={vi.fn()}
        onOpenRun={vi.fn()}
        onOpenGraph={vi.fn()}
      />
    </QueryClientProvider>,
  )
}

function searchResult(snippet: string): SearchResult {
  return {
    id: "res-1",
    source: "github",
    kind: "code",
    relevance: 0.9,
    title: "auth.py",
    url: "github.com/x/auth.py",
    snippet,
    author: "kk",
    timestamp: "2d",
  }
}

function renderCard(snippet: string) {
  return render(
    <ResultCard
      result={searchResult(snippet)}
      num={1}
      expanded={false}
      highlighted={false}
      sources={[]}
      onToggle={vi.fn()}
    />,
  )
}

// ── LandingPanel workspace filter (client-side `?q=`) ────────────────────────

describe("LandingPanel workspace filter", () => {
  const workspaces = [
    workspace({ id: "w1", name: "Platform", desc: "Infra and CI" }),
    workspace({
      id: "w2",
      name: "Design",
      desc: "Figma and specs",
      past_queries: [{ q: "where is AUTH handled", when: "2d ago", results: 4 }],
    }),
  ]

  it("matches name, description, and past-query text case-insensitively", () => {
    renderLanding(workspaces)
    const input = screen.getByLabelText("Filter workspaces")

    fireEvent.change(input, { target: { value: "infra" } })
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Design" })).toBeNull()

    fireEvent.change(input, { target: { value: "auth" } })
    expect(screen.getByRole("heading", { name: "Design" })).toBeInTheDocument()
    expect(screen.queryByRole("heading", { name: "Platform" })).toBeNull()
  })

  it("shows every workspace again when the filter clears", () => {
    renderLanding(workspaces)
    const input = screen.getByLabelText("Filter workspaces")

    fireEvent.change(input, { target: { value: "no-such-workspace" } })
    expect(screen.queryByRole("heading", { name: "Platform" })).toBeNull()
    expect(screen.queryByRole("heading", { name: "Design" })).toBeNull()

    fireEvent.change(input, { target: { value: "" } })
    expect(screen.getByRole("heading", { name: "Platform" })).toBeInTheDocument()
    expect(screen.getByRole("heading", { name: "Design" })).toBeInTheDocument()
  })

  it("exposes a Configure (edit) affordance on EVERY workspace card (#83)", () => {
    const onOpenConfig = vi.fn()
    renderLanding(workspaces, { onOpenConfig })
    // One Configure button per card — the edit dialog is reachable everywhere,
    // not only from the search-bar chip.
    const platformEdit = screen.getByLabelText("Configure workspace Platform")
    const designEdit = screen.getByLabelText("Configure workspace Design")
    expect(platformEdit).toBeInTheDocument()
    expect(designEdit).toBeInTheDocument()

    fireEvent.click(designEdit)
    expect(onOpenConfig).toHaveBeenCalledTimes(1)
    expect(onOpenConfig).toHaveBeenCalledWith(
      expect.objectContaining({ id: "w2", name: "Design" }),
    )
  })
})

// ── ResultCard snippet rendering ─────────────────────────────────────────────

describe("ResultCard snippet rendering", () => {
  it("parses <mark> and <code> tokens into real elements", () => {
    const { container } = renderCard("Use <mark>auth</mark> from <code>core.py</code> here")
    expect(container.querySelector("mark")?.textContent).toBe("auth")
    expect(container.querySelector("p code")?.textContent).toBe("core.py")
  })

  it("renders any other tag as literal text — no raw HTML injection", () => {
    const evil = 'hi <script>alert(1)</script> <img src=x onerror=alert(1)> <mark>ok</mark>'
    const { container } = renderCard(evil)
    expect(container.querySelector("script")).toBeNull()
    expect(container.querySelector("img")).toBeNull()
    expect(container.querySelector("p")?.textContent).toContain("<script>alert(1)</script>")
    expect(container.querySelector("p")?.textContent).toContain("<img src=x onerror=alert(1)>")
    expect(container.querySelector("mark")?.textContent).toBe("ok")
  })

  it("leaves an unclosed token as literal text", () => {
    const { container } = renderCard("an <mark>unclosed token")
    expect(container.querySelector("mark")).toBeNull()
    expect(container.querySelector("p")?.textContent).toBe("an <mark>unclosed token")
  })
})
