// HTTP client for the Agentic Search API. Mirrors the realClient style:
// reads API_BASE / API_KEY from `client.ts` so this module never duplicates
// the auth-header / base-URL logic.

import { API_BASE, API_KEY } from "./client"
import { sseStream } from "./sse"
import type {
  RunPayload,
  RunRecord,
  RunStatus,
  SearchEvent,
  SourceCatalogEntry,
  Workspace,
  WorkspaceInput,
} from "../types/agenticSearch"

function withBase(path: string): string {
  if (!API_BASE) return path
  return `${API_BASE.replace(/\/$/, "")}${path}`
}

function jsonHeaders(): HeadersInit {
  return API_KEY
    ? { "X-API-Key": API_KEY, "Content-Type": "application/json" }
    : { "Content-Type": "application/json" }
}

async function readJson<T>(response: Response): Promise<T> {
  const text = await response.text()
  let data: unknown
  try {
    data = text ? JSON.parse(text) : undefined
  } catch {
    data = undefined
  }
  if (!response.ok) {
    const message =
      (data && typeof data === "object" && "message" in data && typeof (data as { message: unknown }).message === "string"
        ? (data as { message: string }).message
        : null) ?? text ?? `Request failed: ${response.status}`
    throw new Error(message)
  }
  return data as T
}

export async function listSources(): Promise<SourceCatalogEntry[]> {
  const res = await fetch(withBase("/api/agentic_search/sources"), {
    headers: jsonHeaders(),
  })
  const payload = await readJson<{ sources: SourceCatalogEntry[] }>(res)
  return payload.sources
}

export async function listWorkspaces(): Promise<Workspace[]> {
  const res = await fetch(withBase("/api/agentic_search/workspaces"), {
    headers: jsonHeaders(),
  })
  const payload = await readJson<{ workspaces: Workspace[] }>(res)
  return payload.workspaces
}

export async function createWorkspace(input: WorkspaceInput): Promise<Workspace> {
  const res = await fetch(withBase("/api/agentic_search/workspaces"), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  })
  const payload = await readJson<{ workspace: Workspace }>(res)
  return payload.workspace
}

export async function updateWorkspace(
  workspaceId: string,
  input: Partial<WorkspaceInput>
): Promise<Workspace> {
  const res = await fetch(withBase(`/api/agentic_search/workspaces/${encodeURIComponent(workspaceId)}`), {
    method: "PATCH",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  })
  const payload = await readJson<{ workspace: Workspace }>(res)
  return payload.workspace
}

export async function deleteWorkspace(workspaceId: string): Promise<void> {
  const res = await fetch(withBase(`/api/agentic_search/workspaces/${encodeURIComponent(workspaceId)}`), {
    method: "DELETE",
    headers: jsonHeaders(),
  })
  await readJson<unknown>(res)
}

export interface RunInput {
  workspace_id: string
  query: string
  project?: string
  model?: string
}

/** Result of starting a run — the synchronous `POST /runs` envelope. */
export interface StartRunResult {
  run: RunPayload
  run_id: string
  session_id: string
  status: RunStatus
}

/**
 * Start a run. `POST /runs` returns the finished payload synchronously
 * (back-compat) along with `run_id` / `session_id`, which the live stream and
 * reload paths key off.
 */
export async function startRun(input: RunInput): Promise<StartRunResult> {
  const res = await fetch(withBase("/api/agentic_search/runs"), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  })
  return readJson<StartRunResult>(res)
}

/** Fetch a durable run snapshot (reload / deep-link rehydration). */
export async function getRun(runId: string): Promise<RunRecord> {
  const res = await fetch(
    withBase(`/api/agentic_search/runs/${encodeURIComponent(runId)}`),
    { headers: jsonHeaders() }
  )
  const payload = await readJson<{ run: RunRecord }>(res)
  return payload.run
}

/**
 * Stream a run's normalized event log over SSE. Replays from idx 0 then tails
 * until a terminal event. Honors `signal` for clean unmount cancellation.
 */
export function streamRun(
  runId: string,
  options: { signal?: AbortSignal } = {}
): AsyncGenerator<SearchEvent> {
  return sseStream<SearchEvent>(
    `/api/agentic_search/runs/${encodeURIComponent(runId)}/events`,
    { base: API_BASE, apiKey: API_KEY, signal: options.signal }
  )
}
