// HTTP client for the Mewbo Apps API. Mirrors the `agenticSearch.ts` seam:
// reads API_BASE / API_KEY from `client.ts` so this module never duplicates the
// auth-header / base-URL logic. Every request advertises the `apps` capability
// (X-Mewbo-Capabilities) so the create endpoint can scope the builder session
// with the app-building agent/tools — the same two-surface gating the root
// session gets via `realClient.ts`.
//
// The backend ships in a separate workstream (design spec §4A). This module is
// written against the FROZEN REST contract in §4A/§5 so the console works the
// moment the API lands. No endpoints beyond the spec.

import { API_BASE, API_KEY } from "./client";
import { apiFetch } from "./httpBase";
import type { TriggerDTO } from "./triggers";
import type { AppFreshnessWire, AppReadToken, AppReadTokenScope, AppSpec, AppStatus, AppSummary, AppVersion, AppWorkspaceRef, PipelineRun } from "../types/apps";

// The console's app-rendering capability id. Sent alongside the session-scoping
// header so the create endpoint knows this client can render an app.
const APPS_CAPABILITY_ID = "apps";

function withBase(path: string): string {
  if (!API_BASE) return path;
  return `${API_BASE.replace(/\/$/, "")}${path}`;
}

function jsonHeaders(): HeadersInit {
  const base: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Mewbo-Capabilities": APPS_CAPABILITY_ID,
    "X-Mewbo-Surface": "console",
  };
  if (API_KEY) base["X-API-Key"] = API_KEY;
  return base;
}

async function parseBody(response: Response): Promise<{ data: unknown; text: string }> {
  const text = await response.text();
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : undefined;
  } catch {
    data = undefined;
  }
  return { data, text };
}

async function readJson<T>(response: Response): Promise<T> {
  const { data, text } = await parseBody(response);
  if (!response.ok) {
    const message =
      (data && typeof data === "object" && "message" in data && typeof (data as { message: unknown }).message === "string"
        ? (data as { message: string }).message
        : null) ?? text ?? `Request failed: ${response.status}`;
    throw new Error(message);
  }
  return data as T;
}

// ── Response / request envelopes (wrap the wire mirrors in `types.ts`) ────────

/** `GET /api/apps/<id>` — the full app manifest + its version history. */
export interface AppDetail {
  spec: AppSpec;
  versions: AppVersion[];
}

/**
 * A pipeline's schedule — a discriminated union so a
 * `kind` check narrows to the right expression field, mirroring `TriggerKind`'s
 * `"time.at" | "time.cron"` vocabulary (`api/triggers.ts`). `null` means the
 * pipeline has no schedule at all (on-demand, or unscheduled by omission).
 */
export type PipelineSchedule =
  | { kind: "time.cron"; cron: string }
  | { kind: "time.at"; at: string };

/**
 * Per-pipeline liveness row on `/system`.
 * Gives the detail rail a name-keyed view of schedule/on-demand/armed state
 * without re-deriving it from `triggers` + `AppSpec.pipelines.trigger_ref`
 * client-side — the server already did that join for `unscheduled_pipelines`
 * (`_unscheduled_pipelines` in `routes.py`), this just carries the per-row
 * detail alongside it.
 */
export interface PipelineLiveness {
  name: string;
  schedule: PipelineSchedule | null;
  on_demand: boolean;
  trigger_ref: string | null;
  /** Whether `trigger_ref` currently resolves to an armed trigger. */
  armed: boolean;
}

/**
 * `GET /api/apps/<id>/system` — the read-only introspection payload (spec §2.6).
 * The SAME endpoint backs the gallery card's freshness badge AND the detail
 * health strip (DRY), so it carries freshness, the app-scoped triggers (with
 * `next_fire_at`), the recent pipeline-run ledger, and maintainer status.
 */
export interface AppSystemHealth {
  app_id: string;
  status: AppStatus;
  freshness: AppFreshnessWire;
  triggers: TriggerDTO[];
  runs: PipelineRun[];
  maintainer: { session_id: string | null; status: string | null };
  /**
   * Optional/additive: names of pipelines with no trigger armed
   * to run them. Server always emits this (empty list when none), but type
   * it optional anyway and read it defensively — never assume top-level
   * envelope fields are permanent across server versions.
   */
  unscheduled_pipelines?: string[];
  /**
   * Optional/additive: per-pipeline schedule/on-demand/
   * armed detail. Absent on a server that hasn't shipped phase 1 yet — read
   * defensively and fall back to `unscheduled_pipelines` for the coarse
   * warning in that case.
   */
  pipelines?: PipelineLiveness[];
}

/** Body for `POST /api/apps` — the intent composer + workspace choice. */
export interface CreateAppInput {
  intent: string;
  workspace: AppWorkspaceRef;
}

/**
 * `POST /api/apps` result — the draft's id plus the builder session id whose
 * EXISTING SSE event stream the creation flow tails for live build progress
 * until the `app_ready` terminal event.
 */
export interface CreateAppResult {
  app_id: string;
  session_id: string;
}

// ── Reads ─────────────────────────────────────────────────────────────────

/** `GET /api/apps` — the gallery (lean card summaries, no frontend source). */
export async function listApps(): Promise<AppSummary[]> {
  const res = await apiFetch(withBase("/api/apps"), { headers: jsonHeaders() });
  const payload = await readJson<{ apps: AppSummary[] }>(res);
  return payload.apps ?? [];
}

/** `GET /api/apps/<id>` — the manifest + version history for the detail page. */
export async function getApp(appId: string): Promise<AppDetail> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}`), {
    headers: jsonHeaders(),
  });
  return readJson<AppDetail>(res);
}

/** `GET /api/apps/<id>/system` — freshness + triggers + runs + maintainer. */
export async function getAppSystem(appId: string): Promise<AppSystemHealth> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/system`), {
    headers: jsonHeaders(),
  });
  return readJson<AppSystemHealth>(res);
}

// ── Mutations ───────────────────────────────────────────────────────────────

/** `POST /api/apps` — create a draft and spawn its builder session. */
export async function createApp(input: CreateAppInput): Promise<CreateAppResult> {
  const res = await apiFetch(withBase("/api/apps"), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  });
  return readJson<CreateAppResult>(res);
}

/**
 * `POST /api/apps/<id>/token` — mint a short-lived render-scoped token (spec
 * §2.7). Called when the app opens; the token is injected into the served
 * frontend as `_app_context.json`, never exposing the master key. `scope`
 * defaults to `read`; pass `write` only for an app that
 * declares a `user_writable` pipeline — the server 403s a `write` mint for any
 * app that doesn't, so least privilege stays structural.
 */
export async function mintAppToken(
  appId: string,
  scope: AppReadTokenScope = "read",
): Promise<AppReadToken> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/token`), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ scope }),
  });
  return readJson<AppReadToken>(res);
}

/** `POST /api/apps/<id>/pause` — pause the app (pauses its triggers, §2.10). */
export async function pauseApp(appId: string): Promise<AppSpec> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/pause`), {
    method: "POST",
    headers: jsonHeaders(),
  });
  return readJson<AppSpec>(res);
}

/** `POST /api/apps/<id>/resume` — resume the app (re-arms its triggers). */
export async function resumeApp(appId: string): Promise<AppSpec> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/resume`), {
    method: "POST",
    headers: jsonHeaders(),
  });
  return readJson<AppSpec>(res);
}

/** `POST /api/apps/<id>/archive` — archive the app (removes it from the gallery). */
export async function archiveApp(appId: string): Promise<AppSpec> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/archive`), {
    method: "POST",
    headers: jsonHeaders(),
  });
  return readJson<AppSpec>(res);
}

/**
 * `POST /api/apps/<id>/rollback` — repoint the active version to `version`
 * (spec §2.9; history is append-only, so a rollback itself bumps the version).
 * Returns the refreshed detail so the version list re-renders in place.
 */
export async function rollbackApp(appId: string, version: number): Promise<AppDetail> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/rollback`), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ version }),
  });
  return readJson<AppDetail>(res);
}

/**
 * `POST /api/apps/<id>/pipelines/<name>/fire` result ("Run now"). `mode:"code"` ran synchronously (200) — `docs_written`/`cache`/
 * `evaluated_at` describe what just happened. `mode:"agentic"` (202) only
 * STARTED a maintainer run; its result lands later through the existing
 * `/system` poll, never through this response.
 */
export type FirePipelineResult =
  | {
      pipeline: string;
      mode: "code";
      status: "succeeded";
      cache: "hit" | "miss";
      docs_written: Record<string, number>;
      evaluated_at: string;
    }
  | { pipeline: string; mode: "agentic"; status: "started" | "steered" };

/**
 * Thrown by `firePipeline` on any non-2xx (409 not-live/run-in-flight, 429
 * cooldown, 404/503). `retryAfterSeconds` is populated only for a 429 so the
 * UI can render "try again in Ns" — every other status carries just `message`.
 */
export class FirePipelineError extends Error {
  readonly status: number;
  readonly retryAfterSeconds?: number;
  constructor(message: string, status: number, retryAfterSeconds?: number) {
    super(message);
    this.name = "FirePipelineError";
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

/**
 * `POST /api/apps/<id>/pipelines/<name>/fire` — run one pipeline on demand.
 * No body. See {@link FirePipelineResult} / {@link FirePipelineError} for the
 * response shapes.
 */
export async function firePipeline(appId: string, name: string): Promise<FirePipelineResult> {
  const res = await apiFetch(
    withBase(`/api/apps/${encodeURIComponent(appId)}/pipelines/${encodeURIComponent(name)}/fire`),
    { method: "POST", headers: jsonHeaders() },
  );
  const { data, text } = await parseBody(res);
  if (!res.ok) {
    const obj = data && typeof data === "object" ? (data as Record<string, unknown>) : {};
    const message = typeof obj.message === "string" ? obj.message : text || `Request failed: ${res.status}`;
    const retryAfterSeconds = typeof obj.retry_after_seconds === "number" ? obj.retry_after_seconds : undefined;
    throw new FirePipelineError(message, res.status, retryAfterSeconds);
  }
  return data as FirePipelineResult;
}

/** `POST /api/apps/<id>/rearm` result — which pipelines got a freshly armed
 *  schedule trigger, which were already fine, and (when `seed:true` was sent)
 *  which were additionally backfilled. */
export interface RearmResult {
  armed: { pipeline: string; trigger_id: string }[];
  unchanged: string[];
  seeded: string[];
}

/** Body for `POST /api/apps/<id>/rearm`. `seed` additionally backfills
 *  pipelines that have never produced data — omit/false just repairs schedules. */
export interface RearmInput {
  seed?: boolean;
}

/**
 * `POST /api/apps/<id>/rearm` — arm schedule triggers for pipelines the
 * server flagged as unscheduled (repairs the `unscheduled_pipelines` gap).
 * Operator-only, same API key transport as the rest of this module.
 */
export async function rearmApp(appId: string, input: RearmInput = {}): Promise<RearmResult> {
  const res = await apiFetch(withBase(`/api/apps/${encodeURIComponent(appId)}/rearm`), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
  });
  return readJson<RearmResult>(res);
}
