/**
 * Reverse-invocation trigger subsystem — `/api/triggers` + session termination.
 *
 * A *trigger* re-invokes ("wakes") a session when an external condition fires:
 * a wall-clock time, a cron schedule, a CI workflow run, a forge PR event, or a
 * generic webhook. Triggers are created by the agent (`created_by:"agent"`) via
 * the `schedule_trigger` tool; this console surface lists / pauses / resumes /
 * cancels them and terminates the sessions that own them.
 *
 * The backend ships in a later wave — this module is written against the FROZEN
 * REST contract so the page works the moment the API lands.
 * It mirrors the `api/git.ts` seam: typed DTOs + thin `fetch` wrappers over the
 * shared `httpBase` quartet (`withBase`/`authHeaders`/`jsonHeaders`/`readJson`/
 * `readError`) — no third custom fetch wrapper.
 */
import { API_BASE } from "./client";
import {
  apiFetch,
  withBase as sharedWithBase,
  authHeaders,
  jsonHeaders,
  readError,
  readJson,
} from "./httpBase";

// ---------------------------------------------------------------------------
// Closed unions — mirror the backend contract 1:1 so a `Record<…>` presentation
// map (see `triggerFormat.ts`) is exhaustive and `tsc` flags any drift.
// ---------------------------------------------------------------------------

export type TriggerKind =
  | "time.at"
  | "time.cron"
  | "ci.workflow"
  | "forge.pr"
  | "webhook";

export type TriggerStatus =
  | "armed"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired";

/** What firing does: re-engage the existing turn loop, or start a fresh turn. */
export type TriggerAction = "message" | "start";

export type TriggerCreatedBy = "agent" | "user";

/** Who armed the trigger — an agent step, when known. */
export interface TriggerProvenance {
  agent_id?: string;
  step?: number;
}

/** One row of `GET /api/triggers` — the full trigger record. */
export interface TriggerDTO {
  id: string;
  session_id: string;
  kind: TriggerKind;
  status: TriggerStatus;
  /** The prompt the woken session runs when this trigger fires. */
  wake_prompt: string;
  action: TriggerAction;
  /** Kind-specific config (cron expr, ISO time, workflow name, PR ref, …). */
  args: Record<string, unknown>;
  fires: number;
  max_fires?: number | null;
  expires_at?: string | null;
  next_fire_at?: string | null;
  created_at: string;
  created_by: TriggerCreatedBy;
  provenance?: TriggerProvenance | null;
  last_fired_at?: string | null;
  last_error?: string | null;
}

/** Body for `POST /api/sessions/{id}/triggers`. */
export interface CreateTriggerInput {
  kind: TriggerKind;
  wake_prompt: string;
  action?: TriggerAction;
  max_fires?: number;
  expires_at?: string;
  /** Kind-specific config is spread alongside these known keys. */
  args?: Record<string, unknown>;
}

/** Result of `POST /api/sessions/{id}/terminate`. */
export interface TerminateResult {
  session_id: string;
  status: "terminated";
  terminated_at: string;
  /**
   * COUNT of armed triggers the cascade cancelled. The landed backend's
   * `register_on_terminate` seam sums `int` returns (`SessionRuntime`), so this
   * is a number, not a list of ids.
   */
  cancelled_triggers: number;
}

/** Filters accepted by `GET /api/triggers`. */
export interface TriggerListFilters {
  session_id?: string | null;
  kind?: TriggerKind | null;
  status?: TriggerStatus | null;
  limit?: number | null;
}

// ---------------------------------------------------------------------------
// Terminated-session sentinel — a mutation against a terminated session returns
// HTTP 410 `{"error":{"code":"session_terminated",…}}`. We surface that as a
// typed error so the composer can flip to its calm "permanently terminated"
// state instead of showing red error residue.
// ---------------------------------------------------------------------------

export const SESSION_TERMINATED_CODE = "session_terminated";

export class SessionTerminatedError extends Error {
  readonly code = SESSION_TERMINATED_CODE;
  readonly retryable = false;
  readonly reason: string;
  constructor(reason: string) {
    super(reason || "This session has been permanently terminated.");
    this.name = "SessionTerminatedError";
    this.reason = reason;
  }
}

/**
 * True when *err* represents a `session_terminated` rejection — either the
 * typed {@link SessionTerminatedError} thrown by this module, or a generic
 * `Error` whose message carries the 410 envelope (the case for mutations that
 * flow through the shared `api/client` → `httpBase` path, which stringifies the
 * unknown `{error:{…}}` body into `Error.message`). Both are handled so callers
 * on either seam detect termination without re-parsing the wire shape.
 */
export function isSessionTerminatedError(err: unknown): boolean {
  if (err instanceof SessionTerminatedError) return true;
  const message = err instanceof Error ? err.message : String(err ?? "");
  if (!message) return false;
  // Best-effort structured parse first (the httpBase-stringified body), then a
  // substring fallback so a differently-shaped envelope still trips the guard.
  try {
    const parsed = JSON.parse(message) as { error?: { code?: unknown } };
    if (parsed?.error?.code === SESSION_TERMINATED_CODE) return true;
  } catch {
    /* not JSON — fall through to substring check */
  }
  return message.includes(SESSION_TERMINATED_CODE);
}

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/**
 * Read a mutation `Response`, mapping a 410 `session_terminated` body to
 * {@link SessionTerminatedError} and any other non-2xx to the shared
 * `readError` message. On success returns the parsed JSON body.
 */
async function readMutation<T>(response: Response): Promise<T> {
  if (response.status === 410) {
    const body = await response
      .clone()
      .json()
      .catch(() => null);
    const err = (body as { error?: { code?: string; reason?: string } } | null)
      ?.error;
    if (err?.code === SESSION_TERMINATED_CODE) {
      throw new SessionTerminatedError(err.reason ?? "");
    }
    throw await readError(response);
  }
  if (!response.ok) throw await readError(response);
  return readJson<T>(response);
}

// ---------------------------------------------------------------------------
// Reads
// ---------------------------------------------------------------------------

/** GET /api/triggers — all triggers, optionally filtered. */
export async function listTriggers(
  filters: TriggerListFilters = {},
  signal?: AbortSignal,
): Promise<TriggerDTO[]> {
  const params = new URLSearchParams();
  if (filters.session_id) params.set("session_id", filters.session_id);
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.status) params.set("status", filters.status);
  if (filters.limit != null) params.set("limit", String(filters.limit));
  const qs = params.toString();
  const response = await apiFetch(withBase(`/api/triggers${qs ? `?${qs}` : ""}`), {
    headers: authHeaders(),
    signal,
  });
  const data = await readJson<{ triggers?: TriggerDTO[] }>(response);
  return data.triggers ?? [];
}

/** GET /api/sessions/{id}/triggers — triggers owned by one session. */
export async function listSessionTriggers(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TriggerDTO[]> {
  const response = await apiFetch(
    withBase(`/api/sessions/${encodeURIComponent(sessionId)}/triggers`),
    { headers: authHeaders(), signal },
  );
  const data = await readJson<{ triggers?: TriggerDTO[] }>(response);
  return data.triggers ?? [];
}

// ---------------------------------------------------------------------------
// Mutations
// ---------------------------------------------------------------------------

/** PATCH /api/triggers/{id} — pause (`paused`) or resume (`armed`). */
export async function updateTriggerStatus(
  id: string,
  status: "paused" | "armed",
  signal?: AbortSignal,
): Promise<TriggerDTO> {
  const response = await apiFetch(withBase(`/api/triggers/${encodeURIComponent(id)}`), {
    method: "PATCH",
    headers: jsonHeaders(),
    body: JSON.stringify({ status }),
    signal,
  });
  return readMutation<TriggerDTO>(response);
}

/** DELETE /api/triggers/{id} — cancel a trigger. Idempotent. */
export async function cancelTrigger(
  id: string,
  signal?: AbortSignal,
): Promise<{ id: string; status: "cancelled" }> {
  const response = await apiFetch(withBase(`/api/triggers/${encodeURIComponent(id)}`), {
    method: "DELETE",
    headers: authHeaders(),
    signal,
  });
  return readMutation<{ id: string; status: "cancelled" }>(response);
}

/** POST /api/sessions/{id}/terminate — permanent, cascade-cancels triggers. */
export async function terminateSession(
  sessionId: string,
  signal?: AbortSignal,
): Promise<TerminateResult> {
  const response = await apiFetch(
    withBase(`/api/sessions/${encodeURIComponent(sessionId)}/terminate`),
    { method: "POST", headers: jsonHeaders(), signal },
  );
  return readMutation<TerminateResult>(response);
}

/** Trigger statuses that still fire — the only ones pause/resume/cancel act on. */
export const ACTIVE_TRIGGER_STATUSES: ReadonlySet<TriggerStatus> = new Set([
  "armed",
  "paused",
]);

export function isActiveTrigger(t: Pick<TriggerDTO, "status">): boolean {
  return ACTIVE_TRIGGER_STATUSES.has(t.status);
}
