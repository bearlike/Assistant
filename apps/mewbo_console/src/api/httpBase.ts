/**
 * Shared plain-`fetch` helpers for one-shot (non-streaming) API calls:
 * `withBase`/`authHeaders`/`jsonHeaders`/`readJson`/`readError`. Extracted so
 * feature modules stop hand-rolling their own copy of this quartet — before
 * this module existed, `api/git.ts` and `api/realClient.ts` each carried a
 * near-identical set, which is exactly the "custom fetch wrapper" pattern
 * `CLAUDE.md`'s Architecture Constraints forbid.
 *
 * Streaming lives in `sse.ts` (this module doesn't touch that path). The
 * wiki's `<path:slug>`-flavoured `components/wiki/api/client.ts:http<T>` is a
 * separate established seam with its own mock/backend-swap contract — not
 * folded in here either.
 *
 * `realClient.ts` is instantiated per `ApiConfig` (baseUrl/apiKey pair) and
 * threads its own `apiKey` through every call; `git.ts` closes over the
 * `client.ts` singleton and calls these with no `apiKey` argument. So
 * `authHeaders`/`jsonHeaders` take an optional `apiKey` that defaults to the
 * singleton, letting both call styles share one implementation.
 */
import { API_KEY } from "./client";

export function withBase(baseUrl: string, path: string): string {
  if (!baseUrl) return path;
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

// ---------------------------------------------------------------------------
// Credential mode — opt-IN, and only once the server has proven it works
// ---------------------------------------------------------------------------

/**
 * Whether ordinary API calls attach the browser's cookies.
 *
 * `undefined` means "pass nothing to `fetch`", which leaves the platform
 * default (`same-origin`) in force. That default already sends the session
 * cookie on a same-origin/reverse-proxied deployment, so the common case needs
 * no opt-in at all.
 *
 * The cross-origin case is where this has to stay closed by default.
 * `backend.py` answers `Access-Control-Allow-Origin: *` unless `CORS_ORIGIN` is
 * set, and a wildcard origin is *incompatible* with a credentialed request: the
 * browser rejects the response outright. Flipping every call to `include`
 * unconditionally would therefore take a cross-origin, auth-disabled
 * deployment that works today and break all of it. So the mode is only raised
 * by `markCredentialsUsable()` after a credentialed request has actually come
 * back — the server has then demonstrated a non-wildcard origin (which the
 * `auth/kit.py` boot guard enforces whenever a browser authenticator is on).
 */
let credentialsMode: RequestCredentials | undefined;

/**
 * Record that a credentialed request succeeded, so subsequent calls carry
 * cookies too. Called by the auth session probe and nowhere else.
 */
export function markCredentialsUsable(): void {
  credentialsMode = "include";
}

/**
 * `fetch` with the current credentials mode applied.
 *
 * While the mode is unset this is byte-identical to a bare `fetch` (an
 * `undefined` `credentials` is the same as omitting the key), which is what
 * keeps existing deployments untouched.
 */
export function apiFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const credentials = credentialsMode;
  return fetch(input, credentials ? { ...init, credentials } : init);
}

export function authHeaders(apiKey: string | undefined = API_KEY): HeadersInit {
  return apiKey ? { "X-API-Key": apiKey } : {};
}

export function jsonHeaders(apiKey: string | undefined = API_KEY): HeadersInit {
  return { "Content-Type": "application/json", ...authHeaders(apiKey) };
}

async function parseBody(response: Response): Promise<{ text: string; data: unknown }> {
  const text = await response.text();
  const trimmed = text.trim();
  let data: unknown;
  if (trimmed) {
    try {
      data = JSON.parse(trimmed);
    } catch {
      data = undefined;
    }
  }
  return { text, data };
}

/**
 * Fold a pydantic-style `errors` array (`[{loc, msg, ...}]`, e.g. a 422
 * `{"message": "Validation failed", "errors": [...]}` body) into one
 * `"field: reason"`-joined string. Returns `undefined` when `errors` isn't
 * shaped like that, so a caller can fall back to whatever else the body has.
 */
function validationDetails(errors: unknown): string | undefined {
  if (!Array.isArray(errors) || errors.length === 0) return undefined;
  const parts = errors
    .map((entry) => {
      if (!entry || typeof entry !== "object") return undefined;
      const { loc, msg } = entry as Record<string, unknown>;
      const location = Array.isArray(loc) ? loc.join(".") : undefined;
      const reason = typeof msg === "string" ? msg : undefined;
      if (location && reason) return `${location}: ${reason}`;
      return reason ?? location;
    })
    .filter((part): part is string => Boolean(part));
  return parts.length ? parts.join("; ") : undefined;
}

function messageFrom(text: string, data: unknown, status: number): string {
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    const base =
      (typeof obj.message === "string" && obj.message) ||
      (typeof obj.detail === "string" && obj.detail) ||
      undefined;
    const details = validationDetails(obj.errors);
    if (base) return details ? `${base}: ${details}` : base;
    if (details) return details;
  }
  if (data !== undefined) {
    try {
      return JSON.stringify(data);
    } catch {
      return String(data);
    }
  }
  return text || `Request failed: ${status}`;
}

/** Read an error `Response` body into a thrown-ready `Error`. */
export async function readError(response: Response): Promise<Error> {
  const { text, data } = await parseBody(response);
  return new Error(messageFrom(text, data, response.status));
}

/**
 * Extract the human-readable `reason` from the API's structured refusal
 * envelope (`{"error": {code, reason, retryable}}` — see `apps/mewbo_api/CLAUDE.md`,
 * "Access control" → "A refusal is a typed exception"). A body in this shape
 * carries no top-level `message`/`detail`, so `messageFrom` above falls back to
 * `JSON.stringify(data)` — deliberately, since that is what lets a guard like
 * `isSessionTerminatedError` re-parse the original shape out of `Error.message`.
 * A caller that wants the plain string back for DISPLAY re-parses it here
 * rather than teaching every error path a second, structured code path.
 */
export function reasonFrom(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err ?? "");
  if (!message) return message;
  try {
    const parsed = JSON.parse(message) as { error?: { reason?: unknown } };
    if (typeof parsed?.error?.reason === "string" && parsed.error.reason) {
      return parsed.error.reason;
    }
  } catch {
    /* not JSON — the message is already the human-readable string */
  }
  return message;
}

/**
 * Extract the machine-readable `code` from the API's structured refusal
 * envelope (`{"error": {code, reason, retryable}}`), or `""` when the failure
 * carries none. The sibling of {@link reasonFrom}: that one reads what to SHOW
 * a user, this one reads what to BRANCH on.
 *
 * Only a STRING code is returned. `code` is `int | str` on the wire — the
 * app-level 404 handler emits the numeric status — and a caller branching here
 * is always matching a semantic token (`speech_capacity_exhausted`,
 * `session_terminated`), never a status it can read off `Response.status`
 * anyway. Collapsing the numeric case to `""` keeps every call site a plain
 * string compare instead of teaching each one both shapes.
 */
export function codeFrom(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err ?? "");
  if (!message) return "";
  try {
    const parsed = JSON.parse(message) as { error?: { code?: unknown } };
    return typeof parsed?.error?.code === "string" ? parsed.error.code : "";
  } catch {
    /* not JSON — an ordinary Error carries no envelope */
    return "";
  }
}

/** Read a `Response` as JSON; throws (via `readError`'s message logic) on non-2xx. */
export async function readJson<T>(response: Response): Promise<T> {
  const { text, data } = await parseBody(response);
  if (!response.ok) throw new Error(messageFrom(text, data, response.status));
  return (data !== undefined ? data : ({} as T)) as T;
}
