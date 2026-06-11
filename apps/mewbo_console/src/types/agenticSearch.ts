// Wire types for the Agentic Search API. Mirror the Python schema in
// `apps/mewbo_api/src/mewbo_api/agentic_search/`.

/** Coarse run lifecycle — mirrors the BE `RunStatus` literal. */
export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled"

/** Search budget knob (decomposition depth + probe fan-out) — one knob, no
 *  verification rounds. Sent as `tier` on `POST /runs`; default is `auto`. */
export type SearchTier = "fast" | "auto" | "deep"

export interface SourceCatalogEntry {
  id: string
  name: string
  color: string
  bg: string
  glyph: string
  desc: string
  /** Greyed-out when a persisted source is no longer configured. */
  available?: boolean
  unavailable_reason?: string | null
  /** Concrete tool ids this source maps to (tool-scoping seam). */
  tool_ids?: string[]
  /** SCG provider dispatch key (e.g. "mcp_tool_list") for map jobs. */
  source_type?: string
}

export interface PastQuery {
  q: string
  when: string
  results: number
  /** Canonical ISO timestamp; `when` is a coarse human label. */
  ran_at?: string | null
  /** Deep-links the history entry to its run snapshot. */
  run_id?: string | null
  status?: RunStatus | null
}

export interface Workspace {
  id: string
  name: string
  desc: string
  sources: string[]
  instructions: string
  created: string
  /** Canonical ISO timestamps; `created` is the legacy display label. */
  created_at?: string
  updated_at?: string
  past_queries: PastQuery[]
}

export interface WorkspaceInput {
  name: string
  desc: string
  sources: string[]
  instructions: string
}

export interface ResultRef {
  title: string
  url: string
  kind: string
}

export interface ResultInsight {
  label: string
  body: string
}

export interface ResultImage {
  alt: string
  gradient: string
}

export interface ResultEmbed {
  kind: "figma" | "slides"
  title: string
}

export type ResultKind = "docs" | "code" | "threads" | "design" | "tickets" | "web"

export interface SearchResult {
  id: string
  source: string
  kind: ResultKind
  /** Deprecated decorative fake-reveal timer — arrival now comes from SSE. */
  finish_delay_ms?: number | null
  relevance: number
  title: string
  url: string
  snippet: string
  author: string
  timestamp: string
  insight?: ResultInsight | null
  refs?: ResultRef[]
  image?: ResultImage | null
  embed?: ResultEmbed | null
}

export interface TraceLine {
  /** Deprecated decorative fake-reveal timer — arrival now comes from SSE. */
  t_ms?: number | null
  glyph: string
  text: string
  done?: boolean
  empty?: boolean
}

export interface TraceAgent {
  id: string
  agent_id: string
  name: string
  source_id: string
  slot: number // 0..7 — maps to --agent-N tokens
  lines: TraceLine[]
}

export interface AnswerBullet {
  text: string
  cites: string[]
}

export interface RunAnswer {
  tldr: string
  bullets: AnswerBullet[]
  confidence: number
  sources_count: number
}

export interface RelatedPerson {
  name: string
  role: string
  initials: string
  color: number // 0..7 — maps to --agent-N
}

export interface RunPayload {
  run_id: string
  /** Backing session id (BE adds this; optional for un-migrated callers). */
  session_id?: string
  query: string
  workspace_id: string
  status?: RunStatus
  /** Echo of the requested search tier (`POST /runs` body `tier`). */
  tier?: SearchTier
  total_ms: number
  answer: RunAnswer
  results: SearchResult[]
  trace: TraceAgent[]
  related_questions: string[]
  related_people: RelatedPerson[]
  error?: string | null
}

/**
 * Durable run record from `GET /runs/<id>`. The console reads `payload`
 * (a `RunPayload`) for the normalized snapshot; the lifecycle metadata around
 * it powers reload / deep-link / share.
 */
export interface RunRecord {
  run_id: string
  session_id: string
  workspace_id: string
  query: string
  status: RunStatus
  created_at: string
  started_at?: string | null
  completed_at?: string | null
  total_ms: number
  error?: string | null
  source_ids: string[]
  allowed_tools: string[]
  output_contract_version: string
  payload: RunPayload | null
}

// ── SCG (Source Capability Graph) introspection + map jobs ─────────────────

/** Coarse map-job lifecycle — queued → running → completed | failed. */
export type MapJobStatus = "queued" | "running" | "completed" | "failed"

/** Fine-grained SCG map phase (the five-phase map pipeline). */
export type MapJobPhase = "connect" | "introspect" | "parse" | "link" | "finalize"

/** Durable map-source (SCG indexing) job record — mirrors the BE `MapJobRecord`. */
export interface MapJobRecord {
  job_id: string
  source_id: string
  source_type: string
  status: MapJobStatus
  /** Live progress phase; `null` until the first phase event. */
  phase?: MapJobPhase | null
  phase_started_at?: string | null
  node_count: number
  edge_count: number
  /** Redacted `{code, message}` descriptor only — never a secret. */
  error?: { code: string; message: string } | null
  created_at: string
  started_at?: string | null
  completed_at?: string | null
}

/** One mapped source as reported by `GET /scg`. */
export interface ScgSource {
  source_id: string
  source_type: string
}

/** `GET /scg` introspection. `enabled: false` models the 503 "SCG disabled"
 *  response so the console can render a setup hint instead of an error. */
export interface ScgStatus {
  enabled: boolean
  counts: { sources: number; nodes: number; edges: number; recipes: number } | null
  sources: ScgSource[]
}

// ── SSE event protocol ──────────────────────────────────────────────────────
// Discriminated union matching the BE builders in `agentic_search/events.py`.
// `heartbeat` frames are dropped by the transport, so they never reach here.

export interface RunStartedEvent {
  type: "run_started"
  run_id: string
  session_id: string
  workspace_id: string
  query: string
  sources: string[]
}

export interface AgentStartEvent {
  type: "agent_start"
  agent_id: string
  source_id: string
  name: string
  slot: number
}

export interface AgentLineEvent {
  type: "agent_line"
  agent_id: string
  line: TraceLine
}

export interface AgentDoneEvent {
  type: "agent_done"
  agent_id: string
  results_count: number
  empty: boolean
}

export interface ResultEvent {
  type: "result"
  result: SearchResult
}

export interface AnswerDeltaEvent {
  type: "answer_delta"
  text: string
}

export interface AnswerReadyEvent {
  type: "answer_ready"
  answer: RunAnswer
}

export interface RunDoneEvent {
  type: "run_done"
  status: RunStatus
  total_ms: number
}

export interface SearchErrorEvent {
  type: "error"
  error: { code: string; message: string; hint?: string }
}

export interface CancelledEvent {
  type: "cancelled"
}

export type SearchEvent =
  | RunStartedEvent
  | AgentStartEvent
  | AgentLineEvent
  | AgentDoneEvent
  | ResultEvent
  | AnswerDeltaEvent
  | AnswerReadyEvent
  | RunDoneEvent
  | SearchErrorEvent
  | CancelledEvent

/** Event types that terminate the stream. */
export const TERMINAL_SEARCH_EVENTS = ["run_done", "error", "cancelled"] as const

/** Map-job phase update on the map events SSE route. */
export interface MapPhaseEvent {
  type: "phase"
  name: MapJobPhase
}

/** Map-job event stream — phase updates plus the shared terminal events
 *  (the map-job log rides the same `RunSseGenerator` as runs). */
export type MapJobEvent = MapPhaseEvent | RunDoneEvent | SearchErrorEvent | CancelledEvent
