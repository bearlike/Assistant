/**
 * Wiki API client — real HTTP + SSE transport.
 *
 * Single seam between the UI and the backend. All requests go to `/v1/wiki/*`
 * (relative URL; the Vite dev proxy and production same-origin handle routing).
 * Auth: `X-API-Key` header; SSE streams use `?api_key=` query param because
 * EventSource / fetch streaming doesn't support custom headers in all browsers.
 *
 * Contract: every function is a `Promise<T>` or `AsyncGenerator<T>`.
 * Errors throw `WikiError & Error`. UI code never reaches past this module.
 */

import { readRuntimeConfig } from "../../../runtimeConfig";
import { sseStream as genericSseStream } from "../../../api/sse";
import { apiFetch } from "../../../api/httpBase";

import type {
  CatalogDocument,
  CatalogIngestReport,
  IndexingEvent,
  IndexingJob,
  KnowledgeGraph,
  Language,
  Platform,
  Project,
  ProjectFreshness,
  ProjectSettings,
  ProjectSettingsPatch,
  QaAnswer,
  QaEvent,
  RecoverableJob,
  ResumeIndexingResponse,
  SourceExcerpt,
  WikiError,
  WikiPage,
  WikiSessionLink,
  WizardSubmission,
} from "./types";

// ── Auth + base helpers ───────────────────────────────────────────────────

/** Relative base — Vite dev proxy routes `/v1/wiki` → API; prod is same-origin. */
const API_BASE = "";

/** Read the API key at call time so tests can set window.__MEWBO_CONFIG__ without module re-import. */
function getApiKey(): string {
  const rc = readRuntimeConfig();
  return rc?.VITE_API_KEY ?? import.meta.env.VITE_API_KEY ?? "";
}

function makeError(code: WikiError["code"], message: string, hint?: string): WikiError & Error {
  const err = new Error(message) as WikiError & Error;
  err.code = code;
  err.message = message;
  if (hint) err.hint = hint;
  return err;
}

/** Type guard: was this thrown error one of our typed WikiError values? */
export function isWikiError(value: unknown): value is WikiError {
  return Boolean(value && typeof value === "object" && "code" in (value as object));
}

async function http<T>(
  method: string,
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const resp = await apiFetch(API_BASE + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": getApiKey(),
    },
    body: body == null ? undefined : JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    let payload: unknown = null;
    try {
      payload = await resp.json();
    } catch {
      /* non-JSON body */
    }
    const p = payload as Record<string, unknown> | null;
    const code = (p?.code as WikiError["code"]) ?? "internal";
    const msg = (p?.message as string) ?? `HTTP ${resp.status}`;
    const hint = p?.hint as string | undefined;
    throw makeError(code, msg, hint);
  }
  // 204 No Content
  if (resp.status === 204) return undefined as T;
  return (await resp.json()) as T;
}

/**
 * Open an SSE stream via the shared `api/sse.ts` transport, mapping transport
 * errors back into the typed `WikiError` shape the wiki UI expects.
 */
function sseStream<T>(
  path: string,
  opts: { method?: "GET" | "POST"; body?: unknown; signal?: AbortSignal } = {},
): AsyncGenerator<T> {
  return genericSseStream<T>(path, {
    ...opts,
    base: API_BASE,
    apiKey: getApiKey(),
    onError: (_resp, p) =>
      makeError(
        (p?.code as WikiError["code"]) ?? "internal",
        (p?.message as string) ?? "SSE failed",
        p?.hint as string | undefined,
      ),
  });
}

// ── Static defaults (kept locally; no backend round-trip needed) ──────────

export function getDefaultExclusions(): { dirs: string; files: string } {
  return {
    dirs: [
      "node_modules", "dist", "build", "out", ".next", ".git",
      ".venv", "__pycache__", ".idea", ".vscode", "coverage", "target", "vendor",
    ].join("\n"),
    files: [
      "*.lock", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
      "*.min.js", "*.min.css", "*.map", "*.png", "*.jpg", "*.svg", "*.pdf",
    ].join("\n"),
  };
}

// ── Projects ──────────────────────────────────────────────────────────────

export async function listProjects(): Promise<Project[]> {
  return http<Project[]>("GET", "/v1/wiki/projects");
}

export async function deleteProject(slug: string): Promise<{ deleted: boolean }> {
  return http<{ deleted: boolean }>("DELETE", `/v1/wiki/projects/${encodeURIComponent(slug)}`);
}

/**
 * Read a project's editable indexing settings.
 * ``GET /v1/wiki/projects/<slug>/settings``.
 *
 * A catalog (non-git) project comes back as the reduced ``{kind:"catalog", …}``
 * shape. 404 when the slug is unknown.
 */
export async function getProjectSettings(slug: string): Promise<ProjectSettings> {
  if (!slug) throw makeError("validation", "slug is required");
  return http<ProjectSettings>(
    "GET",
    `/v1/wiki/projects/${encodeURIComponent(slug)}/settings`,
  );
}

/**
 * Patch a project's settings. Send ONLY the changed subset — the server
 * forbids unknown keys and never accepts a token or a slug (identity is the
 * URL; credentials live in the registry). Returns the updated settings.
 *
 * Errors: 400 validation · 403 graph-only with developer mode off ·
 * 409 identity-changing repo edit · 404 unknown slug.
 */
export async function updateProject(
  slug: string,
  patch: ProjectSettingsPatch,
): Promise<ProjectSettings> {
  if (!slug) throw makeError("validation", "slug is required");
  return http<ProjectSettings>(
    "PATCH",
    `/v1/wiki/projects/${encodeURIComponent(slug)}`,
    patch,
  );
}

/**
 * Fetch the persisted code knowledge graph for a project. ``limit`` caps
 * the node set (edges whose endpoints aren't in the surviving set are
 * dropped server-side) — the FE always passes one to bound canvas cost
 * for very large graphs. ``hierarchy`` requests the directory-scaffold
 * payload (synthetic ``Folder`` supernodes + ``parentId``/``folderPath``
 * on every node) that powers the 3D galaxy's collapse model.
 */
export async function getKnowledgeGraph(
  slug: string,
  options: { limit?: number; hierarchy?: boolean } = {},
): Promise<KnowledgeGraph> {
  const params = new URLSearchParams();
  if (options.limit != null) params.set("limit", String(options.limit));
  if (options.hierarchy) params.set("hierarchy", "1");
  const qs = params.toString();
  const path = `/v1/wiki/projects/${encodeURIComponent(slug)}/graph${qs ? `?${qs}` : ""}`;
  return http<KnowledgeGraph>("GET", path);
}

/**
 * Fetch a file-source excerpt for a cited Q&A source card. ``start``/``end``
 * are 1-based and optional — when omitted the backend returns the whole file
 * (the caller should still cap rendering). Reuses the same slug encoding as
 * the other per-project routes.
 *
 * ``GET /v1/wiki/projects/<slug>/source?path=<path>&start=<int>&end=<int>``
 */
export async function getSourceExcerpt(
  slug: string,
  path: string,
  start?: number | null,
  end?: number | null,
): Promise<SourceExcerpt> {
  if (!slug) throw makeError("validation", "slug is required");
  if (!path) throw makeError("validation", "path is required");
  const params = new URLSearchParams({ path });
  if (start != null) params.set("start", String(start));
  if (end != null) params.set("end", String(end));
  return http<SourceExcerpt>(
    "GET",
    `/v1/wiki/projects/${encodeURIComponent(slug)}/source?${params.toString()}`,
  );
}

/**
 * Resolve a Mewbo session id to the wiki project it belongs to.
 * ``GET /v1/wiki/sessions/<sessionId>``. A wiki-origin session's context
 * only advertises the ``wiki`` capability, never a project slug — this is
 * the only way the console can resolve one from a bare session id (backs
 * SessionHeader's "Open wiki" jump). 404 → `null`, never a throw.
 */
export async function getWikiSessionLink(sessionId: string): Promise<WikiSessionLink | null> {
  try {
    return await http<WikiSessionLink>(
      "GET",
      `/v1/wiki/sessions/${encodeURIComponent(sessionId)}`,
    );
  } catch (err) {
    if (isWikiError(err) && err.code === "not_found") return null;
    throw err;
  }
}

// ── Catalogue ─────────────────────────────────────────────────────────────

export async function listPlatforms(): Promise<Platform[]> {
  return http<Platform[]>("GET", "/v1/wiki/platforms");
}

export async function listLanguages(): Promise<Language[]> {
  return http<Language[]>("GET", "/v1/wiki/languages");
}

/**
 * Wiki-specific defaults set in app.json. Each key is independent and
 * absent unless the operator pinned a value — the FE overlays whichever
 * fields are present on top of the global fallbacks.
 */
export interface WikiDefaults {
  /** Default model for indexing (wizard). */
  model?: string;
  /** Default model for Q&A. Falls back to ``model`` BE-side if unset. */
  qaModel?: string;
  depth?: "comprehensive" | "concise";
  language?: string;
}

export async function getWikiDefaults(): Promise<WikiDefaults> {
  try {
    return await http<WikiDefaults>("GET", "/v1/wiki/defaults");
  } catch {
    return {};
  }
}

// ── Pages ─────────────────────────────────────────────────────────────────

/**
 * Fetch a wiki page by slug + pageId. The backend returns `{ body, frontmatter, toc, nav }`
 * — the same shape `WikiPage` expects (frontmatter already parsed server-side).
 */
export async function getPage(slug: string, pageId: string): Promise<WikiPage | null> {
  try {
    return await http<WikiPage>(
      "GET",
      `/v1/wiki/projects/${encodeURIComponent(slug)}/pages/${encodeURIComponent(pageId)}`,
    );
  } catch (err) {
    if (isWikiError(err) && err.code === "not_found") return null;
    throw err;
  }
}

// ── Indexing ──────────────────────────────────────────────────────────────

export async function createIndexingJob(
  submission: Partial<WizardSubmission> & { slug: string },
): Promise<IndexingJob> {
  if (!submission.slug) {
    throw makeError("validation", "slug is required");
  }
  return http<IndexingJob>("POST", "/v1/wiki/index", submission);
}

export async function getIndexingJob(jobId: string): Promise<IndexingJob> {
  return http<IndexingJob>("GET", `/v1/wiki/index/${encodeURIComponent(jobId)}`);
}

/**
 * List all non-terminal (queued/scanning/finalizing) indexing jobs.
 * Powers the landing-page "Indexing now" surface. Until the backend
 * route ships this gracefully returns []; the FE never throws.
 */
export async function listActiveJobs(): Promise<IndexingJob[]> {
  try {
    return await http<IndexingJob[]>("GET", "/v1/wiki/jobs/active");
  } catch (err) {
    if (isWikiError(err) && err.code === "not_found") return [];
    return [];
  }
}

export async function cancelIndexingJob(jobId: string): Promise<IndexingJob> {
  return http<IndexingJob>("DELETE", `/v1/wiki/index/${encodeURIComponent(jobId)}`);
}

/**
 * List failed / interrupted / cancelled-but-incomplete jobs that still have
 * reusable work (a graph, some pages). Powers the landing-page "Incomplete
 * indexes" section. Until the backend route ships this gracefully returns
 * []; the FE never throws.
 */
export async function listRecoverableJobs(): Promise<RecoverableJob[]> {
  try {
    return await http<RecoverableJob[]>("GET", "/v1/wiki/jobs/recoverable");
  } catch {
    return []; // any error (route absent, transient) → empty; the FE never throws
  }
}

/**
 * Resume a recoverable indexing job in place, reusing the work it already
 * committed. ``POST /v1/wiki/index/<job_id>/resume`` (empty body) → 202.
 * Errors: 404 not_found, 400 validation (complete / cancelled).
 */
export async function resumeIndexingJob(jobId: string): Promise<ResumeIndexingResponse> {
  if (!jobId) throw makeError("validation", "jobId is required");
  return http<ResumeIndexingResponse>(
    "POST",
    `/v1/wiki/index/${encodeURIComponent(jobId)}/resume`,
  );
}

export async function* subscribeToIndexing(
  jobId: string,
  options: { signal?: AbortSignal } = {},
): AsyncGenerator<IndexingEvent, void, unknown> {
  yield* sseStream<IndexingEvent>(
    `/v1/wiki/index/${encodeURIComponent(jobId)}/stream`,
    { signal: options.signal },
  );
}

// ── Q&A ───────────────────────────────────────────────────────────────────

export async function getAnswer(answerId: string): Promise<QaAnswer> {
  return http<QaAnswer>("GET", `/v1/wiki/qa/${encodeURIComponent(answerId)}`);
}

/**
 * Open a live Q&A stream. `POST /v1/wiki/qa` starts the agent and streams
 * events: `meta` → `summary_ready` → (`block_open`, `block_delta*`, `block_close`)+ → terminal.
 *
 * The consumer (`useQaStream`) drives the typewriter directly from this iterator.
 *
 * Pass ``answerId`` to CONTINUE an existing conversation — the backend appends
 * a turn to that answer's session (reusing its context) instead of minting a
 * fresh answer. Omit it for a new conversation. ``undefined`` fields are
 * dropped by ``JSON.stringify``, so the body carries ``answerId`` only when set.
 */
export async function* streamAnswer(
  input: { question: string; fromPageId: string; model: string; slug: string; answerId?: string },
  options: { signal?: AbortSignal } = {},
): AsyncGenerator<QaEvent, void, unknown> {
  yield* sseStream<QaEvent>("/v1/wiki/qa", {
    method: "POST",
    body: input,
    signal: options.signal,
  });
}

/**
 * Create a Q&A entry and return `{answerId}` by consuming the first `meta`
 * event. Leaves the stream open — callers who want the full typewriter
 * experience should use `streamAnswer` directly.
 */
export async function startAnswer(input: {
  question: string;
  fromPageId: string;
  model: string;
  slug: string;
}): Promise<{ answerId: string }> {
  for await (const ev of streamAnswer(input)) {
    if (ev.type === "meta") return { answerId: (ev as { type: "meta"; answerId: string }).answerId };
    if (ev.type === "error") {
      const wikiErr = (ev as { type: "error"; error: WikiError }).error;
      throw makeError(wikiErr.code, wikiErr.message, wikiErr.hint);
    }
  }
  throw makeError("internal", "no meta event received from QA stream");
}

// ── Catalog (non-git workspace) ───────────────────────────────────────────

/**
 * Ingest one or more documents into a non-git catalog workspace.
 * Creates the project if it does not exist yet (no git URL required).
 *
 * ``POST /v1/wiki/projects/<slug>/documents``
 * Returns 201 with a ``CatalogIngestReport`` on success.
 */
export async function uploadCatalogDocuments(
  slug: string,
  documents: CatalogDocument[],
): Promise<CatalogIngestReport> {
  if (!slug) throw makeError("validation", "slug is required");
  if (!documents.length) throw makeError("validation", "at least one document is required");
  return http<CatalogIngestReport>(
    "POST",
    `/v1/wiki/projects/${encodeURIComponent(slug)}/documents`,
    { documents },
  );
}

// ── Wizard ────────────────────────────────────────────────────────────────

export async function submitWizard(input: WizardSubmission): Promise<IndexingJob> {
  return createIndexingJob({ ...input });
}

/** Request shape for ``POST /v1/wiki/branches``. */
export interface ListBranchesInput {
  repoUrl: string;
  slug?: string;
  token?: string;
}

/** Response shape from ``POST /v1/wiki/branches``. */
export interface ListBranchesResult {
  branches: string[];
  defaultBranch: string | null;
}

/**
 * List the remote's branches so the wizard can offer a branch picker.
 * ``POST /v1/wiki/branches``. Failures surface as the standard WikiError.
 */
export async function listBranches(input: ListBranchesInput): Promise<ListBranchesResult> {
  return http<ListBranchesResult>("POST", "/v1/wiki/branches", input);
}

// ── Refresh ────────────────────────────────────────────────────────────────

/**
 * Request a wiki refresh. ``POST /v1/wiki/projects/<slug>/refresh``.
 * No email collection — refresh is a single-click confirm now.
 */
export async function requestWikiRefresh(slug: string): Promise<{ queued: boolean }> {
  if (!slug) throw makeError("validation", "slug is required");
  return http<{ queued: boolean }>("POST", `/v1/wiki/projects/${encodeURIComponent(slug)}/refresh`, {});
}

/**
 * How far the indexed snapshot has drifted from remote HEAD. ``force`` busts
 * the API's 5-min TTL cache (used by the manual "check again" affordance);
 * ordinary card renders should omit it and let the cache serve.
 */
export async function getProjectFreshness(
  slug: string,
  force = false,
): Promise<ProjectFreshness> {
  if (!slug) throw makeError("validation", "slug is required");
  const qs = force ? "?force=1" : "";
  return http<ProjectFreshness>(
    "GET",
    `/v1/wiki/projects/${encodeURIComponent(slug)}/freshness${qs}`,
  );
}
