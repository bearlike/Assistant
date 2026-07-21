// ---------------------------------------------------------------------------
// Mewbo Apps — LLM-built stlite apps (durable AppSpec entity + per-app data
// plane). These are the console side of the three-way mirror (Python
// `mewbo_api.apps.models` ↔ this file ↔ Kotlin `WidgetFiles`-style DTOs) for
// the wire contracts in the design spec §3. Keys are FROZEN snake_case — never
// rename one here without moving the other two mirrors in lockstep.
//
// Split out of the root `types.ts` (2026-07 burn-down) following the
// `types/agenticSearch.ts` precedent — a self-contained wire-contract cluster
// with its own importers. Consumers import directly from `types/apps`.
// ---------------------------------------------------------------------------

/** Lifecycle status of an app (mirrors `AppSpec.status`). */
export type AppStatus =
  | "draft"
  | "building"
  | "live"
  | "paused"
  | "broken"
  | "archived";

/** Who authored a given `AppVersion` snapshot. */
export type AppVersionAuthor = "builder" | "repair" | "user";

/** Policy reaction to a failed pipeline run (mirrors `AppPolicies`). */
export type AppPipelineFailurePolicy = "repair" | "pause" | "notify";

/** Terminal status of a `PipelineRun` ledger entry. */
export type PipelineRunStatus = "running" | "succeeded" | "failed";

/** Whether an app binds to its own workspace or a shared one. */
export type AppWorkspaceKind = "own" | "shared";

/** The workspace/project primitive an app's agents anchor to (spec §2.3). */
export interface AppWorkspaceRef {
  kind: AppWorkspaceKind;
  key: string;
}

/** One data collection's declared shape (JSON-Schema validated server-side). */
export interface CollectionSpec {
  name: string;
  json_schema: Record<string, unknown>;
  description?: string;
}

/** An agent-authored ingestion/transform pipeline, woken by a trigger. */
export interface PipelineSpec {
  name: string;
  wake_prompt: string;
  /** The trigger id that wakes this pipeline, or null for a manual one. */
  trigger_ref: string | null;
  tools_allowlist: string[];
  /** Agent-owned opaque cursor state (e.g. last-seen id / timestamp). */
  cursor: Record<string, unknown>;
  /**
   * Additive: a `mode="code"` pipeline whose `params` are
   * user input the served frontend may submit (a form, via
   * `app.pipelines.submit`). Its presence on ANY pipeline is what makes the app
   * request a write-scoped render token. Optional/defensive — an older server
   * omits it, which reads (correctly) as "no user-writable pipeline".
   */
  user_writable?: boolean;
}

/** Declarative app policies wired onto existing seams (spec §2.11). */
export interface AppPolicies {
  on_pipeline_failure: AppPipelineFailurePolicy;
  retention_days: number | null;
  max_docs_per_collection: number;
}

/**
 * The multi-file stlite frontend bundle. Generalizes `WidgetReadyPayload.files`
 * from the fixed `{app.py, data.json}` pair to an arbitrary path→source map with
 * an explicit entrypoint. Consumed by the shared stlite boot seam
 * (`widget/stliteBoot.ts:buildAppKernelOptions`).
 */
export interface AppFrontend {
  entrypoint: string;
  files: Record<string, string>;
  requirements: string[];
}

/** A render token's scope: `read` (data/system/pipeline reads) or `write`
 *  (additionally the `app.pipelines.submit` form write-back path). */
export type AppReadTokenScope = "read" | "write";

/**
 * Runtime context injected into a served app as `_app_context.json`: the
 * render-scoped token + API base so the injected Python SDK can reach the
 * per-app data/system namespaces same-origin. NEVER carries the master key
 * (spec §2.7). `scope` tells the SDK whether `app.pipelines.submit` is allowed
 * (absent ⇒ `read`), so a read-token page fails a write attempt client-side.
 */
export interface AppContext {
  token: string;
  api_base: string;
  app_id: string;
  scope?: AppReadTokenScope;
}

/**
 * Multi-file app payload posted to the standalone stlite host over the
 * `mewbo-app-payload` message (Aura + `widget-host.html`). A superset of
 * {@link AppFrontend} carrying the render context. This is a FIXED cross-stream
 * interface — Aura mirrors these keys, so do not rename them.
 */
export interface AppFrontendPayload {
  entrypoint: string;
  files: Record<string, string>;
  requirements: string[];
  app_context: AppContext;
}

/** The durable, versioned app manifest (spec §3, keyed `app_id`). */
export interface AppSpec {
  app_id: string;
  title: string;
  summary: string;
  /** Single emoji used as the app's glyph in the gallery + chrome. */
  icon: string;
  owner_session_id: string;
  workspace_ref: AppWorkspaceRef;
  frontend: AppFrontend;
  collections: CollectionSpec[];
  pipelines: PipelineSpec[];
  policies: AppPolicies;
  maintainer_session_id: string | null;
  version: number;
  status: AppStatus;
  created_at: string;
  updated_at: string;
}

/**
 * Per-version diff summary vs. the prior snapshot — counts + name lists
 * (mirrors Python `AppVersionSummary.compute`). Lives on `AppVersion.summary`;
 * additive/optional, so a version recorded before this field shipped simply
 * lacks one.
 */
export interface AppVersionSummary {
  files_added: number;
  files_changed: number;
  files_removed: number;
  pipelines_added: string[];
  pipelines_removed: string[];
  pipelines_changed: string[];
  collections_added: string[];
  collections_removed: string[];
}

/** Submit-time verification outcome for one pipeline, keyed by pipeline name
 *  on `AppVersion.verification`. */
export type AppVersionVerificationOutcome = "pass" | "fail" | "skipped";

/** One entry in an app's append-only version history (rollback = repoint). */
export interface AppVersion {
  app_id: string;
  version: number;
  /** Full `AppSpec` snapshot at this version. */
  spec: AppSpec;
  author: AppVersionAuthor;
  note?: string;
  /** When this snapshot was recorded (`AppVersion.created_at`, spec §3). */
  created_at: string;
  /** Additive: diff summary vs. the prior version. Absent on older rows. */
  summary?: AppVersionSummary;
  /** Additive: per-pipeline submit-time verification outcome. Absent on
   *  older rows, or when submit-time verification found no pipeline to run. */
  verification?: Record<string, AppVersionVerificationOutcome>;
}

/** A single pipeline run in the provenance ledger (spec §2.8 / §3). */
export interface PipelineRun {
  run_key: string;
  app_id: string;
  pipeline_name: string;
  trigger_id: string | null;
  /** The maintainer run this ledger entry backs; null before it is attributed. */
  session_run_id: string | null;
  started_at: string;
  ended_at: string | null;
  status: PipelineRunStatus;
  /** Docs written per collection during this run. */
  docs_written: Record<string, number>;
  cursor_before: Record<string, unknown>;
  /** Agent-owned opaque cursor after the run; null until the run closes. */
  cursor_after: Record<string, unknown> | null;
  error: string | null;
  /** Additive: declared collection names THIS run wrote zero documents to
   *  (empty unless it succeeded and skipped one). Absent on a row recorded
   *  before this field shipped — every reader must tolerate that. */
  unwritten_collections?: string[];
}

/** Short-lived render-scoped token minted when an app opens (spec §2.7). A
 *  `write` token is minted only for an app declaring a
 *  `user_writable` pipeline; otherwise `read`. */
export interface AppReadToken {
  token_id: string;
  app_id: string;
  scope: AppReadTokenScope;
  expires_at: string;
}

/**
 * `app_ready` — the build-phase terminal event `submit_app` emits onto the
 * builder session's event stream. The console watches for it on the EXISTING
 * session SSE to flip the creation flow into the live app.
 */
export interface AppReadyEvent {
  app_id: string;
  title: string;
  summary: string;
  version: number;
}

/**
 * A gallery card summary (`GET /api/apps`). The lean projection of `AppSpec`
 * that a gallery needs — deliberately WITHOUT `frontend` (a gallery must not
 * ship every app's full source). Freshness + next-fire are fetched per-card
 * from the system endpoint, so they are not carried here.
 */
export interface AppSummary {
  app_id: string;
  title: string;
  summary: string;
  icon: string;
  status: AppStatus;
  version: number;
  workspace_ref: AppWorkspaceRef;
  created_at: string;
  updated_at: string;
}

/**
 * The freshness sub-object of the system-health payload — derived server-side
 * from the `PipelineRun` ledger (spec §2.8). `last_success_at` is the "how
 * fresh is the data" signal, `next_fire_at` the "when does it refresh next",
 * and `stale` is the honest boolean (a data-bearing app whose last run failed
 * or is overdue) so the card never paints a false-green.
 */
export interface AppFreshnessWire {
  last_success_at: string | null;
  last_run_status: PipelineRunStatus | null;
  next_fire_at: string | null;
  stale: boolean;
  /** Additive: declared collection names the MOST RECENT run wrote zero
   *  documents to. `stale` alone reads green whenever the latest run merely
   *  succeeded — this is the other half: a run can succeed while a
   *  collection a served page actually reads never receives a document.
   *  Empty when the run wrote to all of them, or there's no run yet. Absent
   *  on a server predating this field — every reader must tolerate that. */
  unwritten_collections?: string[];
}
