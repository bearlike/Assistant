/**
 * Generic SSE (Server-Sent Events) transport over `fetch`.
 *
 * Extracted from the wiki client (`components/wiki/api/client.ts`) so every
 * feature streams over one battle-tested path. Uses `fetch` rather than
 * native `EventSource` for three reasons the wiki subsystem already proved:
 *
 * 1. `EventSource` can't send custom request headers — the API key rides as a
 *    `?api_key=` query param instead (the API accepts it on SSE routes).
 * 2. `fetch` integrates with `AbortSignal`, so an unmount cleanly cancels.
 * 3. POST payloads with bodies (run-start streams) require `fetch`.
 *
 * Frame format matches the BE builders: `id: <idx>\nevent: <type>\ndata: <json>\n\n`.
 * `heartbeat` frames and empty-data frames are skipped. Each parsed event is
 * yielded as `{ type, ...payload }`, so a discriminated union on `type` works
 * directly in the consumer's reducer.
 */

import { apiFetch } from "./httpBase";

/**
 * Mutable resume cursor shared by one subscriber across reconnects. The parser
 * owns updates because only it sees frame ids; the caller owns its lifetime.
 */
export class SseCursor {
  lastEventId: string | null = null;
}

/** Parse a `fetch` Response body as an SSE stream, yielding typed events. */
export async function* parseSseStream<T>(
  resp: Response,
  signal?: AbortSignal,
  cursor?: SseCursor,
): AsyncGenerator<T> {
  if (!resp.body) throw new Error("SSE response has no body");
  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  let failure: unknown;
  let hasFailure = false;
  try {
    while (true) {
      if (signal?.aborted) break;
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (error) {
        if (signal?.aborted) break;
        throw error;
      }
      const { done, value } = chunk;
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        let type = "message";
        let data = "";
        for (const line of frame.split("\n")) {
          if (line.startsWith("id:")) cursor && (cursor.lastEventId = line.slice(3).trim());
          else if (line.startsWith("event:")) type = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (type === "heartbeat") continue;
        if (!data) continue;
        try {
          const payload = JSON.parse(data) as Record<string, unknown>;
          yield { type, ...payload } as unknown as T;
        } catch {
          // Malformed frame — skip silently.
        }
      }
    }
  } catch (error) {
    failure = error;
    hasFailure = true;
  } finally {
    try {
      await reader.cancel();
    } catch (error) {
      if (!signal?.aborted && !hasFailure) {
        failure = error;
        hasFailure = true;
      }
    }
  }
  if (hasFailure) throw failure;
}

export interface SseOptions {
  method?: "GET" | "POST";
  body?: unknown;
  signal?: AbortSignal;
  /** API key appended as `?api_key=` (EventSource-compatible auth). */
  apiKey?: string;
  /** Optional base URL prefix (empty string = same-origin / dev proxy). */
  base?: string;
  /** Map an error Response into a thrown error. Defaults to a generic Error. */
  onError?: (resp: Response, payload: Record<string, unknown> | null) => Error;
  /** Resume cursor updated from `id:` frames and sent as `Last-Event-ID`. */
  cursor?: SseCursor;
}

/**
 * Open an SSE stream over `fetch` and yield parsed events. `path` may already
 * contain a query string; `api_key` is appended with the correct separator.
 */
export async function* sseStream<T>(
  path: string,
  opts: SseOptions = {},
): AsyncGenerator<T> {
  const { method = "GET", body, signal, apiKey, base = "", onError, cursor } = opts;
  const sep = path.includes("?") ? "&" : "?";
  const keyParam = apiKey ? `${sep}api_key=${encodeURIComponent(apiKey)}` : "";
  const url = base + path + keyParam;
  const resp = await apiFetch(url, {
    method,
    headers: {
      Accept: "text/event-stream",
      ...(cursor?.lastEventId ? { "Last-Event-ID": cursor.lastEventId } : {}),
      ...(body != null ? { "Content-Type": "application/json" } : {}),
    },
    body: body == null ? undefined : JSON.stringify(body),
    signal,
  });
  if (!resp.ok) {
    let payload: Record<string, unknown> | null = null;
    try {
      payload = (await resp.json()) as Record<string, unknown>;
    } catch {
      /* non-JSON body */
    }
    throw onError
      ? onError(resp, payload)
      : new Error(
          (payload?.message as string) ?? `SSE failed: HTTP ${resp.status}`,
        );
  }
  yield* parseSseStream<T>(resp, signal, cursor);
}
