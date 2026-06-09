/**
 * useDraftStream — SSE consumer for POST /v1/draft/stream.
 *
 * Accepts a `DraftStreamInput | null`; while non-null, opens an SSE stream
 * via the shared `sseStream` util, accumulates token deltas into a text
 * string, and exposes `{ text, streaming, done, error }`.
 *
 * Cancellation model:
 *   - An internal `AbortController` is created per effect run; it is aborted
 *     on unmount OR when the serialised input key changes (i.e., whenever the
 *     user submits a new query, the previous stream is cancelled cleanly).
 *   - Setting `input` to `null` clears the stream without opening a new one.
 *
 * Token rendering is driven by arrival, NOT by a synthetic timer. Tokens are
 * appended to `state.text` exactly as they arrive from the network.
 */

import { useEffect, useReducer } from "react";
import { sseStream } from "../api/sse";
import { API_BASE, API_KEY } from "../api/client";
import type { DraftEvent, DraftStreamInput } from "../api/draft";

// ── State & reducer ──────────────────────────────────────────────────────────

export interface DraftStreamState {
  /** Accumulated LLM token text so far. */
  text: string;
  /** True while the stream is open and tokens are arriving. */
  streaming: boolean;
  /** True once the terminal `{done: true}` frame has been received. */
  done: boolean;
  /** Non-null when the fetch or stream parsing threw an error. */
  error: string | null;
}

type DraftAction =
  | { type: "start" }
  | { type: "token"; delta: string }
  | { type: "done" }
  | { type: "error"; message: string }
  | { type: "reset" };

const initialState: DraftStreamState = {
  text: "",
  streaming: false,
  done: false,
  error: null,
};

function reduce(state: DraftStreamState, action: DraftAction): DraftStreamState {
  switch (action.type) {
    case "start":
      return { text: "", streaming: true, done: false, error: null };
    case "token":
      return { ...state, text: state.text + action.delta };
    case "done":
      return { ...state, streaming: false, done: true };
    case "error":
      return { ...state, streaming: false, done: true, error: action.message };
    case "reset":
      return initialState;
  }
}

// ── Hook ─────────────────────────────────────────────────────────────────────

/**
 * Stream LLM token deltas from POST /v1/draft/stream.
 *
 * @param input - Query payload, or `null` to idle / reset.
 * @returns Accumulated text, streaming flag, done flag, and any error message.
 */
export function useDraftStream(input: DraftStreamInput | null): DraftStreamState {
  const [state, dispatch] = useReducer(reduce, initialState);

  // Stable key so the effect only reruns when the caller submits a new request.
  // `null` maps to a sentinel that keeps the effect dormant.
  const key = input
    ? `${input.query}|${input.workspace ?? ""}|${input.model ?? ""}`
    : null;

  useEffect(() => {
    if (!input || !key) {
      dispatch({ type: "reset" });
      return;
    }

    const ctrl = new AbortController();
    let cancelled = false;

    dispatch({ type: "start" });

    (async () => {
      try {
        for await (const frame of sseStream<DraftEvent>("/v1/draft/stream", {
          method: "POST",
          body: input,
          signal: ctrl.signal,
          apiKey: API_KEY,
          base: API_BASE,
        })) {
          if (cancelled) break;
          if ("done" in frame && frame.done) {
            dispatch({ type: "done" });
            break;
          }
          if ("token" in frame && typeof frame.token === "string") {
            dispatch({ type: "token", delta: frame.token });
          }
        }
        // Stream ended without an explicit `done` frame (server closed).
        if (!cancelled) {
          dispatch({ type: "done" });
        }
      } catch (err) {
        if (!ctrl.signal.aborted && !cancelled) {
          const message =
            err instanceof Error ? err.message : String(err);
          dispatch({ type: "error", message });
        }
      }
    })();

    return () => {
      cancelled = true;
      ctrl.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state;
}
