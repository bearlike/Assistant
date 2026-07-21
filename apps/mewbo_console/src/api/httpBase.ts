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

function messageFrom(text: string, data: unknown, status: number): string {
  if (data && typeof data === "object") {
    const obj = data as Record<string, unknown>;
    if (typeof obj.message === "string" && obj.message) return obj.message;
    if (typeof obj.detail === "string" && obj.detail) return obj.detail;
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

/** Read a `Response` as JSON; throws (via `readError`'s message logic) on non-2xx. */
export async function readJson<T>(response: Response): Promise<T> {
  const { text, data } = await parseBody(response);
  if (!response.ok) throw new Error(messageFrom(text, data, response.status));
  return (data !== undefined ? data : ({} as T)) as T;
}
