/**
 * URL-as-source-of-truth tests for the Agentic Search surface (#80 sharability).
 *
 * The canonical URL is `/search?ws=<workspace_id>&run=<run_id>`. Every state
 * that shows a run reflects there, so a copied URL renders the same run +
 * workspace on ANY browser regardless of localStorage. These drive the real
 * `AgenticSearchView` with the API client mocked at the module seam so we can
 * assert both the URL transitions AND which network verbs fire.
 *
 * wouter's `useSearchParams` reads/writes the REAL browser history (jsdom
 * `window.location`), so we seed the URL via `history.pushState` before render
 * and reset it (plus localStorage) between tests.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react"
import { QueryClient, QueryClientProvider } from "@tanstack/react-query"

afterEach(cleanup)

import AgenticSearchView from "../components/agentic_search/AgenticSearchView"
import * as api from "../api/agenticSearch"
import type { RunRecord, Workspace } from "../types/agenticSearch"

vi.mock("../api/agenticSearch", async (orig) => {
  const actual = await orig<typeof import("../api/agenticSearch")>()
  return {
    ...actual,
    listSources: vi.fn(),
    listWorkspaces: vi.fn(),
    listWorkspaceRuns: vi.fn(),
    startRun: vi.fn(),
    getRun: vi.fn(),
    getWorkspaceGraph: vi.fn(),
    // streamRun must yield NOTHING so the SSE reducer stays idle (no fetch) —
    // the snapshot path drives rehydration, mirroring agenticSearchInert.
    streamRun: vi.fn(async function* () {
      /* no events */
    }),
  }
})

const STORAGE_WORKSPACE = "agentic-search:workspace-id"

const wsEng: Workspace = {
  id: "w1",
  name: "Eng",
  desc: "Engineering docs",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  created_at: "2026-05-01T00:00:00Z",
  past_queries: [],
}

const wsOps: Workspace = {
  id: "w2",
  name: "Ops",
  desc: "Operations runbooks",
  sources: ["github"],
  instructions: "",
  created: "May 2026",
  created_at: "2026-05-01T00:00:00Z",
  past_queries: [],
}

/** A durable run snapshot whose workspace is `w2` (the NON-default workspace),
 *  so the run-only deep-link reconcile is observable: a recipient whose
 *  localStorage / first-workspace is `w1` must still land on `w2`. */
const runRecord: RunRecord = {
  run_id: "run-xyz",
  session_id: "s",
  workspace_id: "w2",
  query: "where is auth",
  status: "completed",
  created_at: "2026-05-02T00:00:00Z",
  total_ms: 4200,
  source_ids: ["github"],
  allowed_tools: [],
  output_contract_version: "1.0",
  payload: {
    run_id: "run-xyz",
    query: "where is auth",
    workspace_id: "w2",
    status: "completed",
    total_ms: 4200,
    answer: { tldr: "found in auth.py", bullets: [], confidence: 0.8, sources_count: 1 },
    results: [],
    trace: [],
    related_questions: [],
    related_people: [],
  },
}

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <AgenticSearchView />
    </QueryClientProvider>,
  )
}

/** Current `/search` query string as a `URLSearchParams`. */
function urlParams(): URLSearchParams {
  return new URLSearchParams(window.location.search)
}

beforeEach(() => {
  window.history.pushState({}, "", "/search")
  window.localStorage.clear()
  vi.mocked(api.listSources).mockResolvedValue([
    { id: "github", name: "GitHub", color: "#fff", bg: "#000", glyph: "G", desc: "Code" },
  ])
  vi.mocked(api.listWorkspaces).mockResolvedValue([wsEng, wsOps])
  vi.mocked(api.listWorkspaceRuns).mockResolvedValue([])
  vi.mocked(api.startRun).mockResolvedValue({
    run: {
      run_id: "new-run",
      query: "x",
      workspace_id: "w1",
      status: "completed",
      total_ms: 1,
      answer: { tldr: "", bullets: [], confidence: 0, sources_count: 0 },
      results: [],
      trace: [],
      related_questions: [],
      related_people: [],
    },
    run_id: "new-run",
    session_id: "s",
    status: "completed",
  })
  // Id-aware: `run-xyz` belongs to w2 (deep-link reconcile); the freshly
  // submitted `new-run` belongs to w1 (its submitting workspace) so the
  // post-submit snapshot reconcile is a no-op, not a clobber.
  vi.mocked(api.getRun).mockImplementation(async (id: string) => {
    if (id === "new-run" && runRecord.payload) {
      return {
        ...runRecord,
        run_id: "new-run",
        workspace_id: "w1",
        query: "where is auth",
        payload: { ...runRecord.payload, run_id: "new-run", workspace_id: "w1" },
      }
    }
    return runRecord
  })
  vi.mocked(api.getWorkspaceGraph).mockResolvedValue({
    scope: ["github"],
    nodes: [],
    edges: [],
    stats: {
      totalNodes: 0,
      totalEdges: 0,
      kinds: {},
      perLayer: { schema: 0, memory: 0, entity: 0 },
      unmapped: ["github"],
    },
  })
})

afterEach(() => {
  vi.clearAllMocks()
  window.history.pushState({}, "", "/search")
  window.localStorage.clear()
})

describe("AgenticSearch URL contract (#80 sharability)", () => {
  it("PUSHes ws+run params into the URL on submit success", async () => {
    renderView()
    // Land on the inert landing page (default workspace = w1, first in list).
    const textarea = await screen.findByPlaceholderText(/ask or search the workspace/i)
    fireEvent.change(textarea, { target: { value: "where is auth" } })
    fireEvent.keyDown(textarea, { key: "Enter" })

    await waitFor(() => expect(api.startRun).toHaveBeenCalledTimes(1))
    // The run id arrives async from the POST and lands in the URL alongside the
    // submitting workspace, so the result is immediately shareable.
    await waitFor(() => {
      const p = urlParams()
      expect(p.get("run")).toBe("new-run")
      expect(p.get("ws")).toBe("w1")
    })
  })

  it("reconciles the workspace from the snapshot for a run-only deep-link", async () => {
    // Shared URL carries ONLY `run` — no `ws`. The recipient's first workspace
    // is w1, but the run belongs to w2; the snapshot's workspace_id must win.
    window.history.pushState({}, "", "/search?run=run-xyz")
    renderView()

    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith("run-xyz"))
    // Once the snapshot resolves, `ws` is reconciled into the URL (REPLACE).
    await waitFor(() => expect(urlParams().get("ws")).toBe("w2"))
    expect(urlParams().get("run")).toBe("run-xyz")
    // GET-only rehydration — never a POST.
    expect(api.startRun).not.toHaveBeenCalled()
  })

  it("opening a deep-link never calls the POST mutation", async () => {
    window.history.pushState({}, "", "/search?ws=w2&run=run-xyz")
    renderView()
    await waitFor(() => expect(api.getRun).toHaveBeenCalledWith("run-xyz"))
    // Settle any follow-up effects.
    await waitFor(() => expect(urlParams().get("run")).toBe("run-xyz"))
    expect(api.startRun).not.toHaveBeenCalled()
  })

  it("clearing the run (Back to search) removes `run` but keeps `ws`", async () => {
    // A failed snapshot surfaces the "Back to search" affordance deterministically.
    vi.mocked(api.getRun).mockRejectedValue(new Error("gone"))
    window.history.pushState({}, "", "/search?ws=w2&run=run-xyz")
    renderView()

    const back = await screen.findByRole("button", { name: /back to search/i })
    fireEvent.click(back)

    await waitFor(() => expect(urlParams().get("run")).toBeNull())
    // `ws` survives so the surrounding workspace context is preserved.
    expect(urlParams().get("ws")).toBe("w2")
    expect(api.startRun).not.toHaveBeenCalled()
  })

  it("a bare /search visit (no params) is inert and uses localStorage fallback", async () => {
    window.localStorage.setItem(STORAGE_WORKSPACE, "w2")
    renderView()
    // Landing renders (heading present), no run param written, no POST.
    await screen.findByRole("heading", { name: /agentic search/i })
    expect(urlParams().get("run")).toBeNull()
    expect(api.startRun).not.toHaveBeenCalled()
  })
})
