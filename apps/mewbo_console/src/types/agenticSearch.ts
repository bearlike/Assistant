// Wire types for the Agentic Search API. Mirror the Python schema in
// `apps/mewbo_api/src/mewbo_api/agentic_search/`.

/** Coarse run lifecycle — mirrors the BE `RunStatus` literal. */
export type RunStatus = "queued" | "running" | "completed" | "failed" | "cancelled"

/** Search budget knob (decomposition depth + probe fan-out) — one knob, no
 *  verification rounds. Sent as `tier` on `POST /runs`; default is `auto`. */
export type SearchTier = "fast" | "auto" | "deep"

/** `GET /tiers` — each tier's resolved model preset (the model that actually
 *  drives a run of that tier unless the request carries a `model` override). */
export interface SearchTiersInfo {
  default_tier: SearchTier
  tiers: Record<SearchTier, string>
}

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
  /**
   * How sure the emitting agent is this hit answers the query (0..1) —
   * present on agent-emitted cards only (scg_results entries, #102).
   */
  confidence?: number | null
  title: string
  url: string
  snippet: string
  author: string
  timestamp: string
  insight?: ResultInsight | null
  refs?: ResultRef[]
  image?: ResultImage | null
  embed?: ResultEmbed | null
  /**
   * Structured per-result facts the emitting agent attached — depends on what
   * was retrieved (e.g. GitHub repo `stars`/`forks`, an issue's `state` +
   * `sub_issues`, a HF model's `downloads`/`likes`, a doc's `updated`). The UI
   * renders well-known keys with an icon + compact formatting (`46.2k`,
   * relative time) and unknown keys as `label: value` chips. Additive/optional
   * so old payloads (and connector-era cards) render unchanged.
   */
  meta?: Record<string, string | number | boolean> | null
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
  /** The probe's compressed terminal evidence block (`EVIDENCE (pathway: …)` /
   *  `NO DATA …`) — set from `agent_done.result` so the lane can expand to show
   *  what it actually found. Empty until terminal. */
  result?: string
  /** Agent kind ("coordinator" | "scg-path-probe" | …) — the lane's role; the
   *  `name` becomes the kind on the wire, the model arrives separately. */
  kind?: string
  /** Model that drove this lane (e.g. `claude-sonnet-4-6`) — distinct from the
   *  kind/name. Null/absent on connector-era lanes. */
  model?: string | null
  /** Tool-use steps the lane took (instrument fidelity). */
  steps?: number | null
  /** Wall-clock duration of the lane in ms. */
  duration_ms?: number | null
  /** Billed prompt / completion tokens for the lane. */
  input_tokens?: number | null
  output_tokens?: number | null
  /** KEPT results this lane contributed (after cross-emitter dedup) — mirrors
   *  `agent_done.results_count`. */
  results_count?: number
  /** RAW results this lane emitted before dedup — mirrors
   *  `agent_done.returned_count`. The `returned − kept` delta is the count that
   *  collapsed into another lane's card, surfaced as "N filtered". */
  returned_count?: number
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

/**
 * Honest run-level instrument totals (`payload.stats`). Every field is rendered
 * only when present/non-null — the BE never fabricates, and neither does the UI
 * (the `RunStats`/`RunStatsBlock` honesty rule). `setup_ms` vs `search_ms` split
 * the wall clock into the coordinator's plan/spawn phase and the probe fan-out.
 */
export interface RunStats {
  probes: number
  tool_calls: number
  input_tokens: number
  output_tokens: number
  setup_ms: number | null
  search_ms: number | null
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
  /** Echo of the explicit per-run model override; null/absent = tier default. */
  model?: string | null
  total_ms: number
  answer: RunAnswer
  results: SearchResult[]
  trace: TraceAgent[]
  related_questions: string[]
  related_people: RelatedPerson[]
  /** Run-level instrument totals — additive/optional (old payloads omit it). */
  stats?: RunStats | null
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
  /** Lane kind ("coordinator" | "scg-path-probe" | …) — additive. */
  kind?: string
  /** Model driving the lane — additive; arrives separately from the name. */
  model?: string | null
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
  /** The probe's compressed terminal evidence block (capped server-side). */
  result?: string
  /** RAW results emitted before dedup (additive) — the `returned − results_count`
   *  delta is the lane's "N filtered". */
  returned_count?: number
  /** Per-lane instrument totals (additive) — folded onto the TraceAgent so the
   *  trace rail can show steps/duration/tokens beside the lane name. */
  steps?: number | null
  duration_ms?: number | null
  input_tokens?: number | null
  output_tokens?: number | null
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

/** Follow-up suggestions for the right rail — a parallel structured call at
 *  settle, emitted after `answer_ready` and before the terminal `run_done`. */
export interface RelatedQuestionsEvent {
  type: "related_questions"
  questions: string[]
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
  | RelatedQuestionsEvent
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
