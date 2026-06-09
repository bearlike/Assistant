/**
 * Types for the /v1/draft/stream SSE endpoint.
 *
 * The endpoint accepts a POST body with the query (+ optional workspace /
 * model), then streams LLM token deltas as SSE frames:
 *
 *   data: {"token": "<delta>"}\n\n   (repeated)
 *   data: {"done": true}\n\n         (terminal)
 *
 * Auth rides the shared `?api_key=` query-param convention used by every
 * other SSE route in the console (EventSource-compatible).
 */

/** Request body sent as JSON to POST /v1/draft/stream. */
export interface DraftStreamInput {
  query: string;
  workspace?: string;
  model?: string;
}

/**
 * Discriminated-union frame emitted by the stream.
 *
 * Every real frame is one of:
 *   - `{ token: string }` — incremental LLM token delta (may be an empty
 *     string on some backends; consumers should append unconditionally).
 *   - `{ done: true }`   — terminal sentinel; no more frames follow.
 *
 * The union is open (`& Record<string, unknown>`) because `sseStream`
 * spreads the raw JSON payload — unknown keys from heartbeats / future
 * extensions are ignored by the reducer.
 */
export type DraftEvent =
  | ({ token: string } & Record<string, unknown>)
  | ({ done: true } & Record<string, unknown>);
