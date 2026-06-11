// HTTP client for the Agentic Search API. Mirrors the realClient style:
// reads API_BASE / API_KEY from `client.ts` so this module never duplicates
// the auth-header / base-URL logic.

import { API_BASE, API_KEY } from "./client"
import { sseStream } from "./sse"
import type { WorkspaceGraph } from "../components/agentic_search/graph/types"
import type {
  MapJobEvent,
  MapJobRecord,
  RunPayload,
  RunRecord,
  RunStatus,
  ScgStatus,
  SearchEvent,
  SearchTier,
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
  /** Search budget tier; the server defaults to `scg.default_tier` (auto). */
  tier?: SearchTier
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

/**
 * Fetch the workspace-scoped SCG multiplex graph (#79). Returns the layer-tagged
 * nodes/edges projection (schema + memory + entity), with unmapped sources as
 * ghost nodes. Degrades gracefully server-side — an unmapped / SCG-disabled
 * workspace returns an empty-schema payload (every source in ``stats.unmapped``),
 * never an error; only an unknown workspace 404s.
 */
export async function getWorkspaceGraph(workspaceId: string): Promise<WorkspaceGraph> {
  const res = await fetch(
    withBase(`/api/agentic_search/workspaces/${encodeURIComponent(workspaceId)}/graph`),
    { headers: jsonHeaders() }
  )
  return readJson<WorkspaceGraph>(res)
}

/** List a workspace's persisted run history (most recent first). */
export async function listWorkspaceRuns(workspaceId: string): Promise<RunRecord[]> {
  const res = await fetch(
    withBase(`/api/agentic_search/workspaces/${encodeURIComponent(workspaceId)}/runs`),
    { headers: jsonHeaders() }
  )
  const payload = await readJson<{ runs: RunRecord[] }>(res)
  return payload.runs
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

// ── SCG introspection + map-source (indexing) jobs ──────────────────────────

/**
 * `GET /scg` introspection. The route 503s while `scg.enabled` is off — map
 * that to `{enabled: false}` so the console renders a setup hint, not an error.
 */
export async function getScgStatus(): Promise<ScgStatus> {
  const res = await fetch(withBase("/api/agentic_search/scg"), {
    headers: jsonHeaders(),
  })
  if (res.status === 503) return { enabled: false, counts: null, sources: [] }
  return readJson<ScgStatus>(res)
}

/** Start a map-source (SCG indexing) job. Returns the job record + id. */
export async function startMapJob(
  sourceId: string,
  input: { source_type: string }
): Promise<{ job: MapJobRecord; job_id: string }> {
  const res = await fetch(
    withBase(`/api/agentic_search/sources/${encodeURIComponent(sourceId)}/map`),
    { method: "POST", headers: jsonHeaders(), body: JSON.stringify(input) }
  )
  return readJson<{ job: MapJobRecord; job_id: string }>(res)
}

/** List map jobs for a source, latest-first (reload-safe snapshot read). */
export async function listMapJobs(sourceId: string): Promise<MapJobRecord[]> {
  const res = await fetch(
    withBase(`/api/agentic_search/sources/${encodeURIComponent(sourceId)}/map/jobs`),
    { headers: jsonHeaders() }
  )
  const payload = await readJson<{ jobs: MapJobRecord[] }>(res)
  return payload.jobs
}

/** Fetch a single map-job record. */
export async function getMapJob(
  sourceId: string,
  jobId: string
): Promise<MapJobRecord> {
  const res = await fetch(
    withBase(
      `/api/agentic_search/sources/${encodeURIComponent(sourceId)}/map/jobs/${encodeURIComponent(jobId)}`
    ),
    { headers: jsonHeaders() }
  )
  const payload = await readJson<{ job: MapJobRecord }>(res)
  return payload.job
}

/**
 * Stream a map job's event log over SSE (phase updates + terminal events).
 * `jobId` selects a specific job; otherwise the source's newest job streams.
 */
export function streamMapJob(
  sourceId: string,
  options: { jobId?: string; signal?: AbortSignal } = {}
): AsyncGenerator<MapJobEvent> {
  const qs = options.jobId ? `?job_id=${encodeURIComponent(options.jobId)}` : ""
  return sseStream<MapJobEvent>(
    `/api/agentic_search/sources/${encodeURIComponent(sourceId)}/map/events${qs}`,
    { base: API_BASE, apiKey: API_KEY, signal: options.signal }
  )
}
