// TanStack Query hooks + a small staggered-reveal hook for streaming UX.
// All server state for the Agentic Search page flows through here so the
// view layer never touches fetch directly.

import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"

import {
  createWorkspace,
  deleteWorkspace,
  listSources,
  listWorkspaces,
  runSearch,
  updateWorkspace,
  type RunInput,
} from "../api/agenticSearch"
import type { RunPayload, WorkspaceInput } from "../types/agenticSearch"

const SOURCES_KEY = ["agentic-search", "sources"] as const
const WORKSPACES_KEY = ["agentic-search", "workspaces"] as const

export function useSources() {
  return useQuery({
    queryKey: SOURCES_KEY,
    queryFn: listSources,
    staleTime: Infinity, // catalog is static for the mock; cheap to keep
  })
}

export function useWorkspaces() {
  return useQuery({
    queryKey: WORKSPACES_KEY,
    queryFn: listWorkspaces,
    staleTime: 60_000,
  })
}

export function useCreateWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (input: WorkspaceInput) => createWorkspace(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

export function useUpdateWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, input }: { id: string; input: Partial<WorkspaceInput> }) =>
      updateWorkspace(id, input),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

export function useDeleteWorkspace() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => deleteWorkspace(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

export function useRunSearch() {
  const qc = useQueryClient()
  return useMutation<RunPayload, Error, RunInput>({
    mutationFn: (input) => runSearch(input),
    // Past-queries history lives on the workspace, so a fresh run means
    // the workspaces list is now stale.
    onSuccess: () => qc.invalidateQueries({ queryKey: WORKSPACES_KEY }),
  })
}

/**
 * Tick `elapsed` (ms) from 0 to `totalMs + 600` whenever `nonce` changes.
 * Drives the prototype's client-side staggered reveal: visible results,
 * trace lines, and answer bullets are derived from `elapsed`.
 *
 * Returns 0 (idle) when `nonce` is 0.
 */
export function useStaggeredReveal(nonce: number, totalMs: number): number {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (nonce === 0) return undefined
    setElapsed(0)
    const start = Date.now()
    const id = window.setInterval(() => {
      const e = Date.now() - start
      setElapsed(e)
      if (e > totalMs + 600) {
        window.clearInterval(id)
      }
    }, 50)
    return () => window.clearInterval(id)
  }, [nonce, totalMs])
  return elapsed
}
