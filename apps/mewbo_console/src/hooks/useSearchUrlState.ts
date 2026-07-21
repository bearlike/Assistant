import { useCallback, useEffect, useMemo, useRef } from "react"
import { useSearchParams } from "wouter"

import type { SearchTier, Workspace } from "../types/agenticSearch"

const STORAGE_WORKSPACE = "agentic-search:workspace-id"
const STORAGE_TIER = "agentic-search:tier"

const TIER_VALUES: readonly SearchTier[] = ["fast", "auto", "deep"]

/** Last-used tier, read once at mount so the composer's initial scope
 *  reflects the previous session. */
export function storedTier(): SearchTier {
  if (typeof window === "undefined") return "auto"
  const raw = window.localStorage.getItem(STORAGE_TIER)
  return TIER_VALUES.includes(raw as SearchTier) ? (raw as SearchTier) : "auto"
}

export interface SearchUrlState {
  runId: string | null
  wsParam: string | null
  workspaceId: string | null
  workspace: Workspace | null
  selectWorkspaceParam: (id: string) => void
  openRun: (nextRunId: string, nextWorkspaceId?: string) => void
  clearRun: () => void
}

/**
 * URL IS THE SINGLE SOURCE OF TRUTH for {workspace, active run}.
 * Canonical shape: `/search?ws=<workspace_id>&run=<run_id>`. Both facets are
 * DERIVED from the query string — there is no separate `runId`/`workspaceId`
 * useState — so the URL is deterministic, shareable across browsers, and
 * Back/Forward correct (removing `run` closes the run view; removing `ws`
 * falls back to localStorage). localStorage is only the fallback for a bare
 * `/search` visit; a present `ws` param always wins.
 *
 * INERT INVARIANT: a fresh `/search` visit (no `run` param) lands on the
 * inert landing page and NEVER auto-POSTs. The active run is seeded from the
 * `run` param ONLY; opening any `?run=` URL performs GETs only (snapshot +
 * stream attach) — never a `POST /runs`. Replay = GET snapshot, never re-run.
 */
export function useSearchUrlState(workspaces: Workspace[], tier: SearchTier): SearchUrlState {
  const [searchParams, setSearchParams] = useSearchParams()
  const runId = searchParams.get("run")
  const wsParam = searchParams.get("ws")

  useEffect(() => {
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_TIER, tier)
    }
  }, [tier])

  // Selected workspace id: `ws` param wins; localStorage is the bare-visit
  // fallback. Read lazily once for the fallback so SSR stays safe.
  const storedWorkspaceId =
    typeof window === "undefined" ? null : window.localStorage.getItem(STORAGE_WORKSPACE)
  const workspaceId = wsParam ?? storedWorkspaceId

  // Resolve the current workspace, falling back to the first available one
  // if the selected id is gone or no id has been chosen yet.
  const workspace = useMemo<Workspace | null>(() => {
    if (workspaces.length === 0) return null
    return workspaces.find((w) => w.id === workspaceId) ?? workspaces[0]
  }, [workspaces, workspaceId])

  // Mirror the resolved workspace into localStorage so a later bare `/search`
  // visit (no `ws` param) restores the same selection. The URL param, when
  // present, remains authoritative for the live render.
  useEffect(() => {
    if (!workspace) return
    if (typeof window !== "undefined") {
      window.localStorage.setItem(STORAGE_WORKSPACE, workspace.id)
    }
  }, [workspace])

  // ── URL writers — every {workspace, run} transition reflects into the URL ──
  // Selecting a workspace is NOT a navigation event → REPLACE the `ws` param.
  const selectWorkspaceParam = useCallback(
    (id: string) => {
      setSearchParams(
        (prev) => {
          const next = new URLSearchParams(prev)
          next.set("ws", id)
          return next
        },
        { replace: true }
      )
    },
    [setSearchParams]
  )

  // Opening a run (submit success, replay, runs-chip) IS a navigation event →
  // PUSH `ws`+`run` so browser Back returns to the prior view (landing or run).
  const openRun = useCallback(
    (nextRunId: string, nextWorkspaceId?: string) => {
      setSearchParams((prev) => {
        const next = new URLSearchParams(prev)
        next.set("run", nextRunId)
        if (nextWorkspaceId) next.set("ws", nextWorkspaceId)
        return next
      })
    },
    [setSearchParams]
  )

  // Clearing the active run ("Back to search") is a navigation event → PUSH a
  // run-less URL, keeping `ws` so the surrounding workspace context survives.
  const clearRun = useCallback(() => {
    setSearchParams((prev) => {
      const next = new URLSearchParams(prev)
      next.delete("run")
      return next
    })
  }, [setSearchParams])

  return { runId, wsParam, workspaceId, workspace, selectWorkspaceParam, openRun, clearRun }
}

/**
 * SHARABILITY CORE: a `?run=` URL shared WITHOUT `ws` (or with a
 * mismatched one) must still render the run's own workspace on ANY browser,
 * regardless of the recipient's localStorage. Once the snapshot resolves its
 * `workspace_id` (also carried on the live stream), reconcile `ws` into the
 * URL (REPLACE — this is a derived correction, not a nav event). Reconcile
 * EXACTLY ONCE per run id: after that, a deliberate workspace pick in the
 * results topbar must win — a continuous reconcile would snap it back.
 *
 * Split out from `useSearchUrlState` because the resolved workspace id comes
 * from the run stream/snapshot (`useResolvedRun`), not from the URL itself —
 * the caller threads `resolvedWorkspaceId` through once it's known.
 */
export function useReconcileWorkspaceParam({
  runId,
  wsParam,
  resolvedWorkspaceId,
  selectWorkspaceParam,
}: {
  runId: string | null
  wsParam: string | null
  resolvedWorkspaceId: string | null
  selectWorkspaceParam: (id: string) => void
}): void {
  const reconciledRunRef = useRef<string | null>(null)
  useEffect(() => {
    if (!runId || !resolvedWorkspaceId) return
    if (reconciledRunRef.current === runId) return
    reconciledRunRef.current = runId
    if (wsParam === resolvedWorkspaceId) return
    selectWorkspaceParam(resolvedWorkspaceId)
  }, [runId, resolvedWorkspaceId, wsParam, selectWorkspaceParam])
}
