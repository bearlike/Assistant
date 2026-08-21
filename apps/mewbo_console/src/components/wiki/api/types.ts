/**
 * Wiki API surface — types that the client honours.
 *
 * These are deliberately wire-shaped: a backend implementation can satisfy
 * each function in `client.ts` by returning these shapes verbatim. All UI
 * code reads through this module so the eventual swap is mechanical.
 */

// ── Project (landing card) ────────────────────────────────────────────

export interface Project {
  /** Canonical fully-qualified identity: ``host/owner/repo``, or
   *  two-segment ``owner/repo`` when no host was recorded. */
  slug: string;
  source: "github" | "gitlab" | "bitbucket" | "gitea" | "azure" | "git";
  /** DNS host the repo lives on; null when not recorded. */
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
  /** Git snapshot the wiki was generated from; absent when not recorded.
   *  The `IndexedSnapshot` atomic class hides absent values. */
  branch?: string | null;
  commitSha?: string | null;
  commitShort?: string | null;
  /** True iff the indexed repo carries a maintainer-curated grounder file
   *  (.mewbo/wiki.json or .devin/wiki.json). Sole driver of the
   *  "Maintainer Edited" badge; absent → false. */
  maintainerEdited?: boolean;
  /** True iff the project was indexed in graph-only (developer) mode: the
   *  AST code graph was built with NO documentation pages and NO LLM. Drives
   *  the WikiScreen "No documentation available" empty state. Absent on
   *  ordinary records → treat as false. */
  graphOnly?: boolean;
  /** What exact cross-file symbol resolution achieved for the graph
   *  currently in the store. `null`/absent means the question was never
   *  recorded for this project — read as unknown, never as a healthy pass. */
  resolution?: GraphResolution | null;
}

// ── Graph resolution (code graph fidelity) ─────────────────────────────
//
// Whether the code graph's cross-file edges were resolved exactly or only
// guessed by name — not visible from the graph itself, since an
// unresolved graph is still fully populated and renders fine.

export interface GraphResolution {
  /** False when the resolver could not run at all (its backend isn't
   *  installed on this deployment) — distinct from a resolver that ran and
   *  covered only part of the repository. */
  available: boolean;
  /** Package roots the resolver found in the repository, and how many of
   *  them it indexed. Fewer indexed than discovered is a PARTIAL pass. */
  rootsDiscovered: number;
  rootsIndexed: number;
  /** Cross-file edges contributed by exact resolution. Zero alongside a
   *  complete pass means the repository genuinely has no resolvable
   *  cross-file references in the resolver's language. */
  resolvedEdges: number;
}

// ── Freshness (landing card / project header) ─────────────────────────
//
// Wire shape of ``GET /v1/wiki/projects/<slug>/freshness`` — how far the
// indexed snapshot has drifted from the repo's remote HEAD. Every field is
// nullable because a probe can fail partway: ``ls-remote`` may succeed
// (``remoteSha`` set) while the platform compare API that yields ``behindBy``
// is unavailable. Consumers must treat ``behindBy: null`` as "unknown", not
// "zero".

export interface ProjectFreshness {
  /** Git sha the wiki was generated from (from the Project / latest job). */
  indexedSha: string | null;
  /** Remote HEAD sha from ``git ls-remote``; null when it couldn't be read. */
  remoteSha: string | null;
  /** Commits the indexed snapshot is behind remote HEAD. null = unknown. */
  behindBy: number | null;
  /** True/false when known; null when the check couldn't complete. */
  upToDate: boolean | null;
  /** ISO timestamp the freshness was last computed. */
  checkedAt: string | null;
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
  /** Optional branch/tag to clone; omitted = repo default branch. */
  ref?: string;
  /** Ordered cross-model fallback ladder for the indexing run. Omitted or
   *  empty = no ladder; a non-empty list IS the ladder. */
  fallbackModels?: string[];
  /** Omitted = inherit the deployment's default embedding model. */
  embeddingModel?: string;
  /** Free-text operator guidance appended to the indexer's playbook. Omitted =
   *  no guidance. Capped server-side (4000 chars): it is appended to the prompt
   *  of every page the indexer writes, so it is paid once per page. */
  customInstructions?: string;
  /** External MCP servers to attach for the duration of the index, in the
   *  standard `{name: {command|url, ...}}` MCP config shape. Omitted = attach
   *  nothing. Settable ONLY here and in project settings — an entry names a
   *  process to spawn, so there is deliberately no per-run path. */
  mcpServers?: Record<string, Record<string, unknown>>;
  /** Developer-mode opt-in: build ONLY the AST code graph — no documentation
   *  pages, no LLM. Only honoured by the backend when ``runtime.developer_mode``
   *  is on; omitted entirely otherwise. Default off. */
  graphOnly?: boolean;
}

// ── Editable project settings ────────────────────────────
//
// Wire shape of ``GET /v1/wiki/projects/<slug>/settings`` and the body of
// ``PATCH /v1/wiki/projects/<slug>``. camelCase, like every other wiki wire
// shape (``Project.repoUrl``/``graphOnly``, ``IndexingJob.jobId``) — the server
// keeps snake_case internals behind Pydantic aliases. An early spec draft said
// snake_case; that was lifted from the sessions API and is NOT what shipped.
//
// Settings take effect on the NEXT index — nothing is re-run by a PATCH.

/** One toggle per PATCHable field. The server is authoritative: a field the
 *  server does not flag `true` is NOT offered by the UI (fail-closed — this is
 *  what keeps ``graphOnly``'s developer-mode gate honest on the client too). */
export interface ProjectSettingsEditable {
  model?: boolean;
  fallbackModels?: boolean;
  embeddingModel?: boolean;
  ref?: boolean;
  depth?: boolean;
  language?: boolean;
  filterMode?: boolean;
  dirs?: boolean;
  files?: boolean;
  graphOnly?: boolean;
  customInstructions?: boolean;
  mcpServers?: boolean;
  desc?: boolean;
  /** Flagged ``true`` by the server, and deliberately NOT offered by the UI.
   *  The repo is the project's IDENTITY: the slug keys its pages, jobs and
   *  credentials, so only a cosmetic re-normalisation (``.git`` suffix, scheme
   *  case) is accepted — a real (host, owner, repo) change is a 409, and the
   *  honest way to re-point a wiki is delete + recreate. Rendered read-only. */
  repoUrl?: boolean;
  platform?: boolean;
}

/** The fields the dialog actually renders — the editable set MINUS the
 *  identity fields it deliberately refuses to offer. Keeping these out of the
 *  union is what makes the field→form map exhaustively checkable. */
export type ProjectSettingsField = Exclude<
  keyof ProjectSettingsEditable,
  "repoUrl" | "platform"
>;

/**
 * Read-only status of the credential covering this repo, resolved server-side
 * through the ONE credential chain. The value is NEVER echoed — only
 * whether one is on file and at what scope. The dialog links to the Security
 * facet to change it; it never offers an inline token field.
 */
export interface ProjectSettingsCredential {
  present: boolean;
  scope: string | null;
  scopeType: "host" | "repo" | null;
}

/** Settings for a git-backed project — the full editable surface. */
export interface GitProjectSettings {
  kind?: "git";
  slug: string;
  model: string;
  /** Ordered cross-model fallback ladder. null/absent = no ladder. */
  fallbackModels?: string[] | null;
  /** null/absent = inherit the deployment's default embedding model. */
  embeddingModel?: string | null;
  /** Branch/tag pinned for indexing; null = the repo's default branch. */
  ref: string | null;
  depth: "comprehensive" | "concise";
  language: string;
  filterMode: FilterMode;
  dirs: string[];
  files: string[];
  graphOnly: boolean;
  /** Operator guidance appended to the next index's playbook. null = none. */
  customInstructions?: string | null;
  /** WRITE-ONLY: the GET returns attached server NAMES only, never their
   *  entries — an MCP entry carries credentials in its `env` block and this
   *  route is gated on `wiki.read` while the PATCH is `wiki.admin`. The PATCH
   *  body still takes the full `{name: {...}}` map (see `ProjectSettingsPatch`),
   *  so the form treats it exactly like a secret: show the names, replace the
   *  whole value or leave it alone. */
  mcpServers?: string[] | null;
  desc?: string;
  credential?: ProjectSettingsCredential;
  editable?: ProjectSettingsEditable;
}

/**
 * Reduced shape for a catalog (non-git) project: no clone, so no ref / filters
 * / graph-only. Only the display fields the server flags as editable apply.
 */
export interface CatalogProjectSettings {
  kind: "catalog";
  slug: string;
  model?: string;
  fallbackModels?: string[] | null;
  embeddingModel?: string | null;
  desc?: string;
  credential?: ProjectSettingsCredential;
  editable?: ProjectSettingsEditable;
}

export type ProjectSettings = GitProjectSettings | CatalogProjectSettings;

/** Narrow a settings payload to the reduced catalog shape. */
export function isCatalogSettings(s: ProjectSettings): s is CatalogProjectSettings {
  return s.kind === "catalog";
}

/**
 * Body of ``PATCH /v1/wiki/projects/<slug>``. Only the CHANGED subset is sent
 * (`extra="forbid"` server-side). Never carries `token` or `slug` — identity is
 * the URL, and credentials live in the registry, not here.
 *
 * ``ref: null`` explicitly clears the pinned branch back to the repo default;
 * an absent ``ref`` key leaves it untouched.
 */
export interface ProjectSettingsPatch {
  model?: string;
  /** `null` clears the ladder; an absent key leaves it untouched — the same
   *  omit-vs-null distinction `ref` carries. */
  fallbackModels?: string[] | null;
  /** `null` returns to the deployment default; absent leaves it untouched. */
  embeddingModel?: string | null;
  ref?: string | null;
  depth?: "comprehensive" | "concise";
  language?: string;
  filterMode?: FilterMode;
  dirs?: string[];
  files?: string[];
  graphOnly?: boolean;
  /** `null` clears the guidance; an absent key leaves it untouched. */
  customInstructions?: string | null;
  /** `null` detaches every server; an absent key leaves them untouched. */
  mcpServers?: Record<string, Record<string, unknown>> | null;
  desc?: string;
}

// ── Refresh decision + scope preview ───────────────────────────────────
//
// Mirrors ``mewbo_graph.wiki.types.RefreshDecision`` / ``ScopePreview``
// verbatim (field-for-field, same camelCase aliases) — read those docstrings
// for the full rationale; this file states only what the FE needs to know.

/** The closed vocabulary of fingerprinted fields — what a scoped refresh
 *  compares between the environment that built the prior index and the one
 *  building this one. */
export type FingerprintField =
  | "embedding_model"
  | "graph_schema_version"
  | "grammar_pack_version"
  | "resolver_available";

/** One field where the prior fingerprint and the current one disagree. */
export interface FingerprintMismatch {
  field: FingerprintField;
  expected: string | boolean | null;
  actual: string | boolean | null;
}

/**
 * What a caller may ASK for, as opposed to {@link RefreshDecision}'s ``path``,
 * which is what was CHOSEN. Mirrors ``mewbo_graph.wiki.types.RefreshMode``, and
 * the two vocabularies stay separate on purpose: ``auto`` is not something a job
 * can have run, and there is deliberately no ``"scoped"`` mode — a caller cannot
 * demand reuse of artifacts nothing fingerprinted, so the server 400s it.
 */
export type RefreshMode = "auto" | "full";

/** Why a full rebuild was chosen. Every member is reachable: ``requested``
 *  from the caller's own mode knob, the next two from project state, the
 *  last two from the fingerprint comparison. */
export type RefreshFullReason =
  | "requested"
  | "graph_only"
  | "no_prior_index"
  | "fingerprint_unknown"
  | "fingerprint_mismatch";

/**
 * Which refresh path a project took, and — when it is full — why. A
 * discriminated union on ``path`` so the invariant the backend enforces
 * (``reason`` set IFF ``path === "full"``) is expressible in the type, not
 * just in a comment. ``reason`` on the ``"scoped"`` arm is typed
 * ``null``-or-absent (never a real reason string) because the two BE
 * transports genuinely disagree on which of the two it sends: the refresh
 * route's response dumps the model WITHOUT ``exclude_none`` (literal
 * ``reason: null``), while the job snapshot's ``_job_wire`` dumps WITH it
 * (the key omitted entirely) — verified against both call sites, not
 * assumed. Either way, narrowing to ``path === "full"`` is required to read
 * a real reason. ``mismatches`` rides both arms (it always serialises,
 * defaulting to an empty array) but is only ever non-empty for
 * ``reason === "fingerprint_mismatch"``.
 */
export type RefreshDecision =
  | { path: "scoped"; reason?: null; mismatches: FingerprintMismatch[] }
  | { path: "full"; reason: RefreshFullReason; mismatches: FingerprintMismatch[] };

/**
 * Counts describing what one SCOPED refresh actually touched — present only
 * on a scoped job (absent on every full rebuild: rendering zeros there would
 * claim it examined a scope and found nothing). Deliberately flat, mirroring
 * the backend model, so a reader displays every field verbatim.
 */
export interface ScopePreview {
  filesAdded: number;
  filesModified: number;
  filesDeleted: number;
  /** Files re-parsed whose entity + edge signatures came back identical —
   *  the Salsa early cutoff. High relative to ``filesModified`` means the
   *  diff was mostly comments/formatting and cost nothing downstream. */
  earlyCutoffFiles: number;
  affectedEntities: number;
  memoryKept: number;
  memoryInvalidated: number;
  memoryRevalidated: number;
  pagesKeep: number;
  pagesEdit: number;
  pagesRegenerate: number;
  newPages: number;
  /** LLM calls the deterministic pass actually made — the memory
   *  reconciler's drift band is the only stage that can reach one. Non-zero
   *  here is what makes "free" an honest word rather than an approximate one. */
  llmCalls: number;
}

/**
 * One declared step's observed state. A ledger records the small, ordered plan
 * rather than one entry per processed unit, so snapshots stay bounded while a
 * reader can still see a silent uncountable step as open work.
 */
export interface StepRecord {
  /** Stable dotted identifier for this declared step. */
  key: string;
  /** Human-facing description of the work. */
  label: string;
  /** Coarse rendering bucket — the indexer's phase name. */
  group: string;
  /** Plural noun for counted work; absent means the step is uncountable. */
  unit?: string | null;
  /** Relative share of this operation's declared cost. */
  weight: number;
  state: "pending" | "running" | "done" | "skipped" | "failed";
  startedAt?: string | null;
  endedAt?: string | null;
  current?: number | null;
  total?: number | null;
  /** The last unit the step observed, such as a file path. */
  detail?: string;
  /** Why this step was skipped or failed. */
  note?: string;
}

/**
 * The declared, ordered plan for an indexing operation. The backend owns the
 * weights because they belong to the work itself; consumers render this outline
 * instead of guessing phase shares locally.
 */
export interface ProgressLedger {
  version: 1;
  steps: StepRecord[];
}

export interface IndexingJob {
  jobId: string;
  /** Canonical fully-qualified slug ``host/owner/repo``. */
  slug: string;
  status: IndexingStatus;
  /**
   * Server-derived: true while `status` is queued/scanning/finalizing, or
   * `interrupted` and still awaiting a re-drive (mirrors backend
   * `IndexingJob.is_active`, stamped onto every job at the one `_job_wire`
   * seam). `interrupted` counts as active ON PURPOSE — it means "stopped
   * short of finishing," not "done" — and it flips to false only once the
   * job itself settles to complete/cancelled/failed (including a session
   * TERMINATION cascading into an outright job cancel, rather than leaving
   * a zombie stuck at `interrupted` forever). This is the ONE authority for
   * cancelability; the console must never re-derive it from `status`
   * locally — that duplication is exactly what once hid Cancel behind the
   * very state that made it necessary. Absent only on a pre-rollout
   * snapshot; the console reads that as "not confirmed active" rather than
   * guessing.
   */
  isActive?: boolean;
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
  /** Fine-grained phase from the BE state machine; null when not reported. */
  phase?: IndexingPhase | null;
  /** Total pages from the committed plan; null until commit_plan lands. */
  totalPages?: number | null;
  /** Pages persisted by ``wiki_submit_page`` so far. */
  pagesSubmitted?: number;
  /** ISO timestamp at which the current ``phase`` started — drives ETA. */
  phaseStartedAt?: string | null;
  /**
   * SUPERSEDED by {@link progress}. Kept for an in-flight job and older
   * snapshots; its shared register has no step owner, so new clients prefer
   * the ledger's per-step counters.
   */
  phaseProgressCurrent?: number | null;
  /** SUPERSEDED by {@link progress}. Paired with `phaseProgressCurrent` only
   *  for jobs created before the ledger was available. */
  phaseProgressTotal?: number | null;
  /** SUPERSEDED by {@link progress}. Unit label for the legacy shared
   *  progress triple; absent falls back to a generic label. */
  phaseProgressUnit?: string | null;
  /**
   * Declared indexing plan plus every step's observed state. Absent on older
   * snapshots, where `IndexingProgress` deliberately uses the legacy phase
   * fallback instead of inventing a partial ledger.
   */
  progress?: ProgressLedger;
  /**
   * ISO timestamp of the last progress signal of any kind (not scoped to
   * one phase, unlike {@link phaseStartedAt}). Reserved for a future
   * staleness indicator ("no update in N minutes") that would need a
   * product-specified threshold this type doesn't carry — not consumed by
   * `progress.ts` yet.
   */
  lastProgressAt?: string | null;
  /**
   * The Mewbo session running this index — the indexing screen's "Watch the
   * indexing session" jump. Absent for a graph-only (sessionless) index and
   * for the brief window before the indexer session is attached, so treat its
   * absence as "nothing to watch", never as an error.
   */
  sessionId?: string;
  /** Git snapshot resolved at clone time — surfaced mid-flight on the indexing screen. */
  branch?: string | null;
  commitSha?: string | null;
  /** Set on `failed` jobs only. */
  error?: WikiError;
  /**
   * Which refresh path THIS job took, and why — stamped once at creation,
   * never rewritten. Absent means the job is a FIRST index rather than a
   * refresh, which is a third state and not the same as "full": nothing was
   * reused because there was nothing to reuse yet.
   */
  refreshDecision?: RefreshDecision;
  /**
   * The committed scope of a scoped refresh, written through the same
   * ``emit_*`` seam that writes the {@link IndexingEvent} `scope_preview`
   * event — one write, two transports, so the two can never disagree.
   * Absent on every full rebuild AND on a first index.
   */
  scopePreview?: ScopePreview;
}

export type IndexingStatus =
  | "queued"
  | "scanning"
  | "finalizing"
  | "interrupted"
  | "complete"
  | "cancelled"
  | "failed";

/**
 * A terminal-but-incomplete indexing job that still has reusable work —
 * surfaced in the landing page's "Incomplete indexes" section. Returned by
 * ``GET /v1/wiki/jobs/recoverable``.
 */
export interface RecoverableJob {
  jobId: string;
  /** Canonical fully-qualified slug ``host/owner/repo``. */
  slug: string;
  /** Terminal-but-incomplete status (failed / interrupted / cancelled). */
  status: IndexingStatus;
  /** Phase the run reached before stopping. */
  phase?: IndexingPhase | null;
  /** Terminal error the job carried, if any (the backend sends the WikiError
   *  object, not a bare string — render `error.message`). */
  error?: WikiError | null;
  /** Pages persisted before the run stopped. */
  pagesSubmitted?: number;
  /** Total pages from the committed plan; null until commit_plan landed. */
  totalPages?: number | null;
  /** ISO timestamp of the last update. */
  updatedAt?: string | null;
  /** Reusable work a resume will skip / pick up from. */
  recoverable: {
    /** Page ids / phases already done that a resume will skip. */
    skip: string[];
    pagesDone: number;
    pagesRemaining: number;
    nodeCount: number;
  };
}

/** 202 response shape from ``POST /v1/wiki/index/<job_id>/resume``. */
export interface ResumeIndexingResponse {
  jobId: string;
  sessionId: string;
  status: IndexingStatus;
}

/**
 * Response shape from ``GET /v1/wiki/sessions/<sessionId>`` — resolves a
 * Mewbo session id to the wiki artifact it belongs to. A 404 means the
 * session isn't a wiki session at all; the client maps that to `null` rather
 * than throwing.
 *
 * A DISCRIMINATED UNION, not one shape with a `kind` label, because the two
 * kinds have genuinely different destinations: an indexing session belongs to
 * a PROJECT (route via its `landingPageId`), a Q&A session belongs to one
 * ANSWER, which is separately addressable (`?answer=<id>`). Typing it this way
 * is what makes `tsc` require the consumer to branch instead of quietly
 * routing both to the project's front door. The `indexing` arm is unchanged.
 */
export type WikiSessionLink =
  | {
      kind: "indexing";
      slug: string;
      /** The job this session is indexing — the indexing screen's address.
       *  Optional: a server older than this console does not send it, and the
       *  caller must fall back to the project rather than route to `undefined`. */
      jobId?: string;
      /** `IndexingJob.is_active`. A LIVE run wants the progress bar; a finished
       *  one wants the project, which is why the destination is not fixed. */
      active?: boolean;
    }
  | {
      kind: "qa";
      slug: string;
      /** The persisted answer this session generated — the deep-link target. */
      answerId: string;
      /** Page the answer was generated from; captions it and backs the up-link. */
      fromPageId: string;
      /** Latest turn's question. Empty on answers persisted before it was stored. */
      question: string;
    };

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
  | "enrich"
  | "plan"
  | "pages"
  | "finalize";

export interface IndexingLogEntry {
  level: "info" | "warn" | "error";
  text: string;
  /**
   * Declared step this line was emitted inside — the backend stamps the open
   * step onto every log it writes, and absent means the work ran outside any
   * declared scope. The activity pane groups consecutive lines by it, which is
   * the whole reason the backend carries it: without the key a reader gets
   * thousands of undifferentiated lines and no way to tell which step produced
   * which.
   */
  step?: string | null;
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
  /** Snapshot of the declared plan and its derived whole-operation numbers.
   *  `steps` lets a consumer that does not need grouping still render the
   *  ordered outline; `groups` preserves the backend's phase grouping. */
  | {
      type: "progress";
      version: 1;
      fraction: number;
      etaSeconds: number | null;
      elapsedSeconds: number | null;
      activeKey: string | null;
      groups: Array<{ key: string; steps: StepRecord[] }>;
      steps: StepRecord[];
    }
  /** Emitted once, at the end of a SCOPED refresh's delta pass — the SAME
   *  counts as {@link IndexingJob.scopePreview}, carried inline (never
   *  nested under a sub-key) so folding it onto the job snapshot is a
   *  straight spread. Never emitted on a full rebuild or a first index. */
  | ({ type: "scope_preview" } & ScopePreview)
  /** A timeline line, attributed to the declared step that was open when the
   *  backend wrote it. `step` is absent for work outside any declared scope
   *  and for a job older than the ledger. */
  | { type: "log"; level: "info" | "warn" | "error"; text: string; step?: string | null };

// ── Q&A ────────────────────────────────────────────────────────────────

/** Terminal (or in-flight) state of a single Q&A turn. */
export type QaTurnStatus = "running" | "complete" | "cancelled" | "error";

/**
 * How much work a Q&A run is allowed to do. Chosen per conversation at the
 * composer and sent on the opening ``POST /v1/wiki/qa``; the backend derives it
 * for every follow-up on the same answer, so a continuation never re-picks it.
 * Omitted on the wire means ``fast`` — the server default.
 */
export type QaMode = "fast" | "deep";

/**
 * One PRIOR turn of a multi-turn Q&A conversation. The backend returns these
 * oldest-first in ``QaAnswer.turns``; the CURRENT/latest turn is described by
 * the top-level ``QaAnswer`` fields instead. A follow-up reuses the same
 * ``answerId`` (and backend session), appending a turn rather than minting a
 * new answer.
 */
export interface QaTurn {
  /** The question that opened this turn. */
  question: string;
  blocks: Block[];
  /** The LLM's curated, human-facing citation list for this turn. */
  summarySources: string[];
  /** Deterministic provenance trail for this turn — see ``QaAnswer.accessedSources``. */
  accessedSources?: string[];
  /** Distinct LLM models that ran across this turn's hypervisor + probes. */
  modelsUsed?: string[];
  status: QaTurnStatus;
}

export interface QaAnswer {
  /** Stable id assigned by the backend; used for shareable QA URLs. Stays the
   *  SAME across every follow-up in a conversation — it addresses the whole
   *  growing multi-turn thread, not a single turn. */
  answerId: string;
  /** Page id this answer was generated from, used to caption the summary. */
  fromPageId: string;
  /** The CURRENT/latest turn's question. Absent on a single-turn answer with
   *  no stored follow-ups. */
  question?: string;
  /** PRIOR completed turns, oldest first. Empty/absent for a never-followed-up
   *  answer. The top-level fields describe the LATEST turn, so old code paths
   *  that read only those keep working. */
  turns?: QaTurn[];
  /** The LLM's curated, human-facing citation list (LATEST turn). */
  summarySources: string[];
  /** Authoring model — used by the "Generated with…" pill. */
  model: string;
  blocks: Block[];
  /**
   * Deterministic provenance trail: every graph node / source file / wiki
   * page the hypervisor's probes actually touched. Citation-id grammar:
   * ``graph:<node_id>``, ``<path>#L<a>-<b>`` (or bare ``<path>``),
   * ``wiki:<page_id>``. Distinct from the curated ``summarySources``.
   * Absent on older answers — treat as ``[]``.
   */
  accessedSources?: string[];
  /**
   * Distinct LLM models that ran across the hypervisor + its probes
   * (e.g. ``["openai/claude-sonnet-4-6", "openai/haiku"]``). Absent on
   * older answers — treat as ``[]``.
   */
  modelsUsed?: string[];
  /**
   * The Mewbo session that generated this answer — backs the Q&A screen's
   * "Watch the answering session" jump on a replayed (``?answer=``) load.
   * Stamped at read time by the snapshot route (the same seam `IndexingJob`'s
   * `sessionId` uses), so it is ABSENT rather than empty when the answer has
   * no backing session: treat absence as "nothing to watch", never an error.
   */
  sessionId?: string;
  /** Which Q&A agent shape ran — see ``QaMode``. Absent on answers persisted
   *  before this field existed; treat as ``deep`` (every such answer ran the
   *  hypervisor + probe fan-out, the only shape that existed then). */
  mode?: QaMode;
}

/**
 * A file-source excerpt for a single cited source card. Returned by
 * ``GET /v1/wiki/projects/<slug>/source?path=&start=&end=``. ``content`` is
 * the raw excerpt text (the requested window, or the whole file when no
 * range is given); ``startLine`` is the 1-based line number of the first
 * line of ``content`` so the viewer can number it correctly, and
 * ``endLine`` the last. ``totalLines`` is the file's full length.
 */
export interface SourceExcerpt {
  path: string;
  startLine: number | null;
  endLine: number | null;
  totalLines: number;
  content: string;
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
  | { type: "meta"; answerId: string; model: string; fromPageId: string; sessionId?: string }
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
// Wire shape returned by ``GET /v1/wiki/projects/<slug>/graph``. The
// ``{nodes,edges}`` arrays feed straight into the shared 3D ``Graph3DView``
// (and its ``CollapseModel`` for the ``?hierarchy=1`` folder LOD).

export type GraphNodeKind =
  | "File"
  | "Module"
  | "Class"
  | "Function"
  | "Method"
  | "Interface"
  // ── Extended symbol kinds (schema v2) ──
  // ``Object`` is a singleton (Kotlin ``object`` / ``companion object``);
  // ``Property`` is a field / property / constant. Both ride the ``ast`` layer
  // and render as code discs, distinguished only by colour like the others.
  | "Object"
  | "Property"
  // ── Multiplex layers (wire contract v2) ──
  // ``External`` is an AST node for a cross-file/import target the graph
  // now resolves and shares; ``Entity`` and ``Memory`` are the abstract
  // and memory-orchestration layers respectively.
  | "External"
  | "Entity"
  | "Memory"
  // ── Hierarchy scaffold (``?hierarchy=1``) ──
  // ``Folder`` is a synthetic directory supernode (id ``folder:<path>``,
  // layer ``ast``) that folds its subtree via ``CONTAINS`` edges — the
  // spatial scaffold AND the level-of-detail collapse mechanism for the
  // 3D galaxy. Collapsed folders render as a single sphere.
  | "Folder";

export type GraphEdgeKind =
  | "CONTAINS"
  | "IMPORTS"
  | "CALLS"
  | "EXTENDS"
  | "REFERENCES"
  // ── Multiplex layers (wire contract v2) ──
  // ``ANCHORS`` is the cross-layer edge tying an entity/memory node to its
  // AST anchor; ``RELATES`` is the intra-layer edge for entity & memory
  // graphs (carries an optional verb ``label``).
  | "ANCHORS"
  | "RELATES";

/** Multiplex layer a node belongs to (wire contract v2). */
export type GraphLayer = "ast" | "entity" | "memory";

/** Multiplex layer an edge belongs to — ``cross`` is the inter-layer tie. */
export type GraphEdgeLayer = GraphLayer | "cross";

export interface KnowledgeGraphNode {
  data: {
    id: string;
    label: string;
    kind: GraphNodeKind;
    /** Open per-kind refinement (schema v2): e.g. ``companion``
     *  for a Kotlin companion object, ``const`` for a constant. Absent when
     *  the node carries no refinement (every current AST kind). */
    subkind?: string;
    /** Multiplex layer. Absent on AST-only jobs. */
    layer?: GraphLayer;
    /** AST nodes only — absent on entity/memory nodes. */
    file?: string;
    range?: [number, number];
    docstring?: string;
    /** Entity nodes only — e.g. ``concept`` | ``role`` | ``user-story``. */
    entityType?: string;
    /** Entity / memory nodes — free-form classifier labels. */
    labels?: string[];
    /** Memory nodes only — the stored snippet. */
    snippet?: string;
    /** Hierarchy scaffold (``?hierarchy=1``): id of the enclosing
     *  ``Folder`` supernode, or ``null`` for a visible root. Absent on
     *  flat (non-hierarchy) payloads. */
    parentId?: string | null;
    /** Hierarchy scaffold: the directory path this node sits under (for
     *  folders, their own path). Absent on flat payloads. */
    folderPath?: string | null;
  };
}

export interface KnowledgeGraphEdge {
  data: {
    id: string;
    source: string;
    target: string;
    kind: GraphEdgeKind;
    /** Multiplex layer. Absent on AST-only jobs. */
    layer?: GraphEdgeLayer;
    /** Verb label carried by ``RELATES`` entity edges. */
    label?: string;
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
    /** Per-layer node tallies. Absent when not reported. */
    perLayer?: Partial<Record<GraphLayer, number>>;
    /** Count of synthetic ``Folder`` supernodes in a ``?hierarchy=1``
     *  payload. Absent on flat payloads. */
    folderCount?: number;
  };
}

// ── Graph selection contract (3D galaxy ↔ inspector) ──────────────────
//
// The shared seam between the 3D galaxy screen (which emits a selection on a
// node/edge click) and ``GraphInspector`` (which renders it). A ``folder`` /
// ``file`` / ``module`` / ``symbol`` / ``external`` / ``entity`` / ``memory``
// selection carries a ``node``; an ``edge`` selection carries the clicked
// ``edge`` plus, for a collapsed supernode↔supernode tie, the ``aggregated``
// constituent edges that were folded into it.

/** Coarse selection bucket. ``symbol`` collapses Class/Function/Method/
 *  Interface — the inspector renders them with one symbol layout. */
export type GraphSelectionKind =
  | "folder"
  | "file"
  | "module"
  | "symbol"
  | "external"
  | "entity"
  | "memory"
  | "edge";

export interface GraphSelection {
  kind: GraphSelectionKind;
  node?: KnowledgeGraphNode;
  edge?: KnowledgeGraphEdge;
  /** Constituent edges folded into an aggregated supernode↔supernode tie. */
  aggregated?: KnowledgeGraphEdge[];
}

// ── Catalog (non-git workspace) ───────────────────────────────────────

/**
 * A single document to ingest into a non-git catalog workspace.
 * ``id`` must be unique within the batch; ``title`` becomes the page
 * title and ``text`` the raw content. Optional ``metadata`` is
 * forwarded verbatim for embedding / retrieval filtering.
 */
export interface CatalogDocument {
  id: string;
  title: string;
  text: string;
  metadata?: Record<string, string>;
}

/**
 * 201 response shape from ``POST /v1/wiki/projects/<slug>/documents``.
 * Mirrors the backend ``CatalogIngestResponse`` wire contract.
 */
export interface CatalogIngestReport {
  /** Canonical project slug (created if absent). */
  slug: string;
  /** Number of documents accepted into the batch. */
  ingested: number;
  /** Documents that were also embedded (may be < ingested on partial failure). */
  embedded: number;
  /** Running total of documents in this workspace after the call. */
  totalDocuments: number;
  /** Documents indexed via BM25 only (embedding quota / short text). */
  bm25Only: number;
  /** Wiki landing page id for the new workspace. */
  landingPageId: string;
}

// ── Wizard state type (used across wizard + catalog paths) ────────────

/** Discriminates the two wizard source paths. */
export type WizardSourceType = "git" | "catalog";

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
