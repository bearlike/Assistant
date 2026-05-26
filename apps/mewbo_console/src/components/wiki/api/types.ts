/**
 * Wiki API surface — types that the client honours.
 *
 * These are deliberately wire-shaped: a backend implementation can satisfy
 * each function in `client.ts` by returning these shapes verbatim. All UI
 * code reads through this module so the eventual swap is mechanical.
 */

// ── Project (landing card) ────────────────────────────────────────────

export interface Project {
  /** Canonical fully-qualified identity: ``host/owner/repo``. Legacy
   *  records may still be two-segment ``owner/repo``. */
  slug: string;
  source: "github" | "gitlab" | "bitbucket" | "gitea" | "azure" | "git";
  /** DNS host the repo lives on; null on legacy records only. */
  host?: string;
  lang: string;
  indexedAt: string;
  pages: number;
  primary?: boolean;
  desc: string;
  /** Page id to land on when the tile is clicked. */
  landingPageId?: string;
  /** Canonical repo URL — preferred over slug-derived ``https://host/...``. */
  repoUrl?: string;
  /** Git snapshot the wiki was generated from. Legacy records have these
   *  absent; the `IndexedSnapshot` atomic class hides absent values. */
  branch?: string | null;
  commitSha?: string | null;
  commitShort?: string | null;
  /** True iff the indexed repo carries a maintainer-curated grounder file
   *  (.mewbo/wiki.json or .devin/wiki.json). Sole driver of the
   *  "Maintainer Edited" badge — legacy records default to false. */
  maintainerEdited?: boolean;
}

// ── Platforms (wizard) ────────────────────────────────────────────────

export interface Platform {
  id: "github" | "gitlab" | "bitbucket" | "gitea" | "azure" | "git";
  name: string;
  mono: string;
  color: string;
  short: string;
  hosts: string[];
  tokenLabel: string;
  tokenScope: string;
  tokenUrl: string | null;
  tokenSteps: string[];
}

// ── Models (wizard + Q&A dock) ────────────────────────────────────────
//
// Same wire shape as the main app's `/api/models` — a flat string list of
// provider-prefixed model IDs (`anthropic/claude-sonnet-4-5`,
// `openai/gpt-5-mini`, …). Brand icons resolve via the shared
// `getProviderIcon(modelId)` helper (substring match) and display labels
// strip the provider prefix via `formatModelName`. Keeping this in lockstep
// means swapping `/api/models` between the two endpoints is mechanical.

// ── Languages (wizard) ────────────────────────────────────────────────

export interface Language {
  id: string;
  label: string;
  subtle?: string;
}

// ── Wiki content (sidebar / pages / TOC / diagrams) ───────────────────

export interface NavEntry {
  id: string;
  label: string;
  lvl: 1 | 2 | 3;
  parent?: string;
}

export interface TocEntry {
  id: string;
  label: string;
  lvl: 1 | 2 | 3;
}

// Inline rich-text nodes
export type InlineNode =
  | string
  | InlineNode[]
  | { code: string }
  | { link: string; text: string }
  | { kind: "src"; path: string; lines?: string };

// Block types
export type Block =
  | { kind: "p"; text: InlineNode }
  | { kind: "h2"; id?: string; text: string }
  | { kind: "h3"; id?: string; text: string }
  | { kind: "hr" }
  | { kind: "ul"; items: InlineNode[] }
  | { kind: "accordion"; title: string; items: string[] }
  | { kind: "sources"; items: string[] }
  | { kind: "table"; head: string[]; rows: InlineNode[][] }
  | { kind: "diagram"; id: string };

export interface WikiPage {
  id: string;
  title: string;
  /** Parsed frontmatter (title/slug/sources/etc.). */
  frontmatter: {
    title: string;
    slug: string;
    relevantSources?: Array<{ path: string; lines?: string }>;
    sources?: Array<{ path: string; lines?: string }>;
  };
  /** Markdown body with frontmatter already stripped. */
  body: string;
  /** Auto-derived from headings (override via frontmatter.tocOverride). */
  toc: TocEntry[];
  /** Sidebar nav tree (same for every page in a wiki). */
  nav: NavEntry[];
}

// ── Wizard submission (production POST shape) ─────────────────────────

export type FilterMode = "exclude" | "include";

export interface WizardSubmission {
  repoUrl: string;
  slug: string;
  platform: Platform["id"];
  token?: string;
  depth: "comprehensive" | "concise";
  language: string;
  model: string;
  filterMode: FilterMode;
  dirs: string[];
  files: string[];
}

export interface IndexingJob {
  jobId: string;
  /** Canonical fully-qualified slug ``host/owner/repo``. */
  slug: string;
  status: IndexingStatus;
  scannedCount: number;
  totalCount: number;
  currentFile: string | null;
  /** When complete, the page id to land on. */
  landingPageId?: string;
  /** Platform of record — drives the brand icon and API endpoint shape. */
  platform?: Platform["id"];
  /** DNS host the repo lives on (denormalized from slug for convenience). */
  host?: string;
  /** Model the indexer is using — surfaced on the loader for transparency. */
  model?: string;
  /** Fine-grained phase from the BE state machine (null on legacy jobs). */
  phase?: IndexingPhase | null;
  /** Total pages from the committed plan; null until commit_plan lands. */
  totalPages?: number | null;
  /** Pages persisted by ``wiki_submit_page`` so far. */
  pagesSubmitted?: number;
  /** ISO timestamp at which the current ``phase`` started — drives ETA. */
  phaseStartedAt?: string | null;
  /** Git snapshot resolved at clone time — surfaced mid-flight on the indexing screen. */
  branch?: string | null;
  commitSha?: string | null;
  /** Set on `failed` jobs only. */
  error?: WikiError;
}

export type IndexingStatus =
  | "queued"
  | "scanning"
  | "finalizing"
  | "complete"
  | "cancelled"
  | "failed";

/**
 * Discriminated event union streamed by `subscribeToIndexing`.
 *
 * Transport contract:
 *   - Mock: yielded via an async generator, one event per `yield`.
 *   - Backend: Server-Sent Events. Each event MUST be encoded as
 *     `event: <type>\ndata: <json-without-the-type-field>\n\n`. The
 *     frontend's SSE consumer (a future swap-in for the generator)
 *     re-assembles `{ type, ...data }` from those two lines.
 *
 * Ordering guarantees:
 *   1. `queued` is ALWAYS the first event on a fresh subscription. On
 *      mid-job re-subscribe the backend MAY emit `queued` again followed
 *      by zero or more `scanned` catch-up events, then resume live.
 *   2. Each file in the scan plan produces exactly one `scanning` followed
 *      by exactly one `scanned`, both carrying the file's `index`. Files
 *      are reported in plan order; indexes are monotonic.
 *   3. `finalizing` is emitted at most once, after the last `scanned`.
 *   4. Exactly one terminal event closes the stream: `complete`,
 *      `cancelled`, or `error`. After a terminal event the SSE
 *      connection SHOULD be closed by the server.
 *   5. `heartbeat` may appear anywhere; consumers MUST ignore it. It
 *      exists so proxies don't kill idle connections (real SSE needs
 *      this every 15–25s).
 *
 * Cancellation:
 *   The frontend either (a) aborts its AbortSignal — closes the SSE
 *   without informing the server (used on route change / unmount), OR
 *   (b) calls `DELETE /v1/wiki/index/:jobId` — server marks the job
 *   cancelled and the SSE flushes a `cancelled` event before closing.
 *   Both paths are idempotent.
 */
/**
 * Coarse indexing phase. Drives the phase-weighted progress bar — each
 * phase has a real weight; sub-progress within a phase comes from
 * ``scanned``/``totalCount`` (scan) or ``pagesSubmitted``/``totalPages``
 * (pages). No more 0→96% jumps.
 */
export type IndexingPhase =
  | "clone"
  | "scan"
  | "graph"
  | "plan"
  | "pages"
  | "finalize";

export interface IndexingLogEntry {
  level: "info" | "warn" | "error";
  text: string;
  /** Wall-clock seconds since epoch when this line was emitted (UI sort key). */
  ts?: number;
}

export type IndexingEvent =
  | { type: "queued"; jobId: string; slug: string; totalCount: number }
  | { type: "scanning"; file: string; index: number; totalCount: number }
  | { type: "scanned"; file: string; index: number; totalCount: number }
  | { type: "finalizing"; scannedCount: number; totalCount: number }
  | { type: "complete"; landingPageId: string; pageCount: number }
  | { type: "cancelled" }
  | { type: "error"; error: WikiError }
  | { type: "heartbeat" }
  | { type: "phase"; name: IndexingPhase }
  | { type: "plan_committed"; totalPages: number }
  | { type: "page_committed"; pageId: string; index: number; totalPages: number }
  | { type: "log"; level: "info" | "warn" | "error"; text: string };

// ── Q&A ────────────────────────────────────────────────────────────────

export interface QaAnswer {
  /** Stable id assigned by the backend; used for shareable QA URLs. */
  answerId: string;
  /** Page id this answer was generated from, used to caption the summary. */
  fromPageId: string;
  summarySources: string[];
  /** Authoring model — used by the "Generated with…" pill. */
  model: string;
  blocks: Block[];
}

/**
 * Discriminated event union streamed by `streamAnswer`. Each event is
 * additive — the consumer never needs the previous state to interpret
 * one. Production transport: SSE (`event: type\ndata: <json>\n\n`).
 *
 * Ordering guarantees:
 *   `meta` always first (carries `answerId` + the chosen `model`).
 *   `summary_ready` arrives once, before any `block_*` event.
 *   `block_open` opens a block at `index`; `block_delta` appends to that
 *     index's text portion; `block_close` finalises it. Per-block events
 *     are strictly in order of their `index`.
 *   `complete` ends the stream cleanly. `error`/`cancelled` end it too.
 */
export type QaEvent =
  | { type: "meta"; answerId: string; model: string; fromPageId: string }
  | { type: "summary_ready"; sources: string[] }
  | { type: "block_open"; index: number; block: Block }
  | { type: "block_delta"; index: number; textAppend: string }
  | { type: "block_close"; index: number }
  | { type: "complete"; totalBlocks: number }
  | { type: "cancelled" }
  | { type: "error"; error: WikiError }
  | { type: "heartbeat" };

// ── Knowledge graph (viewer) ──────────────────────────────────────────
//
// Wire shape returned by ``GET /v1/wiki/projects/<slug>/graph``. Each
// node/edge is already Cytoscape-ready — the consumer can pass the
// arrays straight to ``cy.add(elements)``.

export type GraphNodeKind =
  | "File"
  | "Module"
  | "Class"
  | "Function"
  | "Method"
  | "Interface";

export type GraphEdgeKind =
  | "CONTAINS"
  | "IMPORTS"
  | "CALLS"
  | "EXTENDS"
  | "REFERENCES";

export interface KnowledgeGraphNode {
  data: {
    id: string;
    label: string;
    kind: GraphNodeKind;
    file: string;
    range: [number, number];
    docstring: string;
  };
}

export interface KnowledgeGraphEdge {
  data: {
    id: string;
    source: string;
    target: string;
    kind: GraphEdgeKind;
  };
}

export interface KnowledgeGraph {
  slug: string;
  nodes: KnowledgeGraphNode[];
  edges: KnowledgeGraphEdge[];
  stats: {
    nodeCount: number;
    edgeCount: number;
    kinds: Partial<Record<GraphNodeKind, number>>;
    /** Total node count BEFORE any ``?limit=`` cap was applied. */
    totalNodes?: number;
    /** Total edge count BEFORE orphan filtering. */
    totalEdges?: number;
    /** ``true`` when a node cap dropped real nodes. Orphan-edge
     *  filtering on its own does NOT set this. */
    truncated?: boolean;
  };
}

// ── Errors ─────────────────────────────────────────────────────────────

/**
 * Typed error model. The mock raises these and SSE/HTTP transports map
 * them onto status codes + JSON payloads.
 *
 * Codes are stable strings the UI can switch on — `not_found` for 404
 * shapes, `forbidden` for token/scope issues, `rate_limited` for 429,
 * `internal` for 5xx, `network` for transport-level failures, etc.
 */
export interface WikiError {
  /**
   * Stable code the UI switches on. Map to HTTP status codes:
   *   not_found      → 404
   *   forbidden      → 403  (auth scope; token missing/insufficient)
   *   repo_access    → 502  (couldn't clone — host down, branch gone)
   *   quota_exceeded → 429  (per-user/index-job/QA-tokens quota hit)
   *   rate_limited   → 429  (transient — Retry-After header honoured)
   *   validation     → 400  (use `fields` for per-field UI messages)
   *   cancelled      → 499  (client-issued cancel — see IndexingEvent docs)
   *   internal       → 5xx  (catch-all server fault)
   *   network        → no HTTP — transport itself failed (CORS / DNS / SSE close)
   */
  code:
    | "not_found"
    | "forbidden"
    | "repo_access"
    | "quota_exceeded"
    | "rate_limited"
    | "validation"
    | "cancelled"
    | "internal"
    | "network";
  message: string;
  /** Optional remediation hint shown verbatim to the user. */
  hint?: string;
  /** Field-level errors for `validation` failures (e.g. wizard URL). */
  fields?: Record<string, string>;
  /** Seconds to wait before retrying — only set for `rate_limited`. */
  retryAfter?: number;
}
