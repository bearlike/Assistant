/**
 * The two SSE stream consumers (`useIndexingStream`, `useQaStream`) — split
 * out of `hooks.ts` so that file stays a plain TanStack Query surface. Each
 * owns an `AbortController` per mount, so unmounting or starting a new
 * request cancels the in-flight stream in both the mock and the production
 * transport via the same surface.
 */

import { useEffect, useMemo, useReducer, useRef } from "react";

import { SseCursor } from "@/api/sse";

import { streamAnswer, subscribeToIndexing } from "./client";
import type {
  Block,
  IndexingEvent,
  IndexingJob,
  IndexingLogEntry,
  IndexingPhase,
  ProgressLedger,
  QaEvent,
  QaMode,
  WikiError,
} from "./types";

// ── Streaming hooks ─────────────────────────────────────────────────

/** Exported for direct unit testing, mirroring `SessionStreamState`'s
 *  precedent (`src/hooks/useSessionEvents.ts`) — a plain state shape a test
 *  seeds directly rather than scripting an async transport. */
export interface IndexingStreamState {
  /** Current job snapshot, folded from incoming events. */
  job: IndexingJob | null;
  /** True once this subscriber has folded a terminal stream event. */
  terminal?: boolean;
  /** Rolling list of scan history rows for the UI. */
  history: Array<{ name: string; done: boolean }>;
  /** Current coarse phase from the BE state machine — null until the
   *  first ``phase`` event arrives (some backends never emit it). */
  phase: IndexingPhase | null;
  /** Total pages from the committed plan; null until commit_plan lands. */
  totalPages: number | null;
  /** Pages persisted by ``wiki_submit_page`` so far. */
  pagesSubmitted: number;
  /** Free-form milestone log lines for the indexing timeline. */
  logs: IndexingLogEntry[];
  /** Latest terminal error, if any. */
  error: WikiError | null;
}

const initialIndexingState: IndexingStreamState = {
  job: null,
  history: [],
  phase: null,
  totalPages: null,
  pagesSubmitted: 0,
  logs: [],
  error: null,
  terminal: false,
};

/** Exported for direct unit testing (`SessionStreamState`-style testable pure
 *  unit) — the hook itself is exercised through `useIndexingStream` below. */
export function reduceIndexing(state: IndexingStreamState, event: IndexingEvent): IndexingStreamState {
  switch (event.type) {
    case "queued":
      return {
        ...initialIndexingState,
        job: {
          jobId: event.jobId,
          slug: event.slug,
          status: "queued",
          scannedCount: 0,
          totalCount: event.totalCount,
          currentFile: null,
        },
      };
    case "scanning": {
      const next = state.job
        ? { ...state.job, status: "scanning" as const, currentFile: event.file, scannedCount: event.index }
        : null;
      const history = [...state.history, { name: event.file, done: false }];
      return { ...state, job: next, history };
    }
    case "scanned": {
      const next = state.job
        ? { ...state.job, scannedCount: event.index + 1, currentFile: null }
        : null;
      const history = state.history.map((h) => (h.name === event.file ? { ...h, done: true } : h));
      return { ...state, job: next, history };
    }
    case "finalizing":
      return state.job
        ? {
            ...state,
            job: {
              ...state.job,
              status: "finalizing",
              currentFile: null,
              scannedCount: event.scannedCount,
              totalCount: event.totalCount,
            },
          }
        : state;
    case "heartbeat":
      return state;
    case "complete":
      return state.job
        ? {
            ...state,
            phase: "finalize",
            job: {
              ...state.job,
              status: "complete",
              currentFile: null,
              landingPageId: event.landingPageId,
            },
            terminal: true,
          }
        : state;
    case "cancelled":
      return state.job
        ? { ...state, job: { ...state.job, status: "cancelled", currentFile: null }, terminal: true }
        : { ...state, terminal: true };
    case "error":
      return { ...state, error: event.error, terminal: true };
    case "phase":
      return { ...state, phase: event.name };
    case "plan_committed":
      return { ...state, totalPages: event.totalPages };
    case "page_committed":
      return {
        ...state,
        pagesSubmitted: event.index + 1,
        totalPages: event.totalPages || state.totalPages,
      };
    case "progress": {
      // The event carries derived numbers too, but the shared IndexingProgress
      // class derives from the ledger so snapshot and stream use one path.
      const progress: ProgressLedger = { version: event.version, steps: event.steps };
      return state.job ? { ...state, job: { ...state.job, progress } } : state;
    }
    case "scope_preview": {
      // Fold the SAME counts the snapshot's `scopePreview` field carries — the
      // event and the field are one write through the BE's shared `emit_*`
      // seam, so this is the ONLY place the stream half needs to know that.
      // `queued` always precedes `scope_preview` (ordering guarantee on
      // `IndexingEvent`), so `state.job` is set by the time this arrives; the
      // guard is defensive only.
      const { type: _type, ...preview } = event;
      return state.job ? { ...state, job: { ...state.job, scopePreview: preview } } : state;
    }
    case "log":
      return {
        ...state,
        logs: [
          ...state.logs,
          // `step` is folded rather than dropped: the backend stamps the open
          // declared step onto every line it writes, and it is what lets the
          // activity pane group thousands of lines into readable runs. A
          // producer whose field no consumer reads is dead on arrival.
          { level: event.level, text: event.text, step: event.step ?? null, ts: Date.now() / 1000 },
        ],
      };
  }
}

/**
 * Consume the indexing event stream into folded UI state. Handles
 * cancellation via an internal `AbortController` that fires on unmount
 * or when `jobId` changes.
 *
 * ``resubscribeKey`` lets a caller force a fresh subscription on the SAME
 * job without unmounting — bump it after a resume so the screen re-opens
 * the stream in place (the BE replays from idx 0 with ``queued`` first,
 * which resets the folded state).
 */
export function useIndexingStream(jobId: string | null, resubscribeKey = 0) {
  const [state, dispatch] = useReducer(reduceIndexing, initialIndexingState);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!jobId) return;
    const ctrl = new AbortController();
    const cursor = new SseCursor();
    controllerRef.current = ctrl;
    let cancelled = false;
    let terminal = false;
    const waitForRetry = (delay: number) => new Promise<void>((resolve) => {
      const timeout = window.setTimeout(resolve, delay);
      ctrl.signal.addEventListener("abort", () => {
        window.clearTimeout(timeout);
        resolve();
      }, { once: true });
    });
    (async () => {
      let delay = 250;
      while (!cancelled && !ctrl.signal.aborted && !terminal) {
        try {
          for await (const event of subscribeToIndexing(jobId, { signal: ctrl.signal, cursor })) {
            if (cancelled) break;
            dispatch(event);
            if (event.type === "complete" || event.type === "cancelled" || event.type === "error") {
              terminal = true;
              break;
            }
          }
          if (!terminal && !cancelled && !ctrl.signal.aborted) {
            await waitForRetry(delay);
            delay = Math.min(delay * 2, 2_000);
          }
        } catch (err) {
          if (ctrl.signal.aborted || cancelled) break;
          // A closed transport is recoverable while the job itself is open.
          // Keep the stream error-free and retry from the parser's last id.
          await waitForRetry(delay);
          delay = Math.min(delay * 2, 2_000);
          if (!ctrl.signal.aborted && !cancelled && delay >= 2_000) {
            // The retry remains live; only a terminal event makes this visible
            // as a job error, because the generator self-closes while work runs.
            void err;
          }
        }
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [jobId, resubscribeKey]);

  // Bound the file-scan history (a side panel that only ever shows
  // recent activity) but keep the full ``logs`` list intact — the
  // indexing page renders all of them inside a scroll container and
  // pins to the bottom. A rolling-window slice here made each refresh
  // appear to show "different logs" as the underlying total grew.
  const trimmedHistory = useMemo(() => state.history.slice(-9), [state.history]);
  return { ...state, history: trimmedHistory };
}

// ── QA streaming ─────────────────────────────────────────────────────

interface QaStreamState {
  answerId: string | null;
  model: string | null;
  fromPageId: string | null;
  /**
   * The Mewbo session answering this turn, carried on `meta` — what backs the
   * Q&A screen's jump into the run WHILE it streams (the persisted snapshot
   * carries the same binding for a replayed answer). The event defaults it to
   * `""` so an older persisted `meta` replays unchanged, so an empty string is
   * normalised to `null` HERE rather than at each reader: absence is "nothing
   * to watch", and two spellings of it would eventually disagree.
   */
  sessionId: string | null;
  /** "Generated from … and related sources" chips. */
  summarySources: string[] | null;
  blocks: Block[];
  done: boolean;
  cancelled: boolean;
  error: WikiError | null;
}

const initialQaState: QaStreamState = {
  answerId: null,
  model: null,
  fromPageId: null,
  sessionId: null,
  summarySources: null,
  blocks: [],
  done: false,
  cancelled: false,
  error: null,
};

function reduceQa(state: QaStreamState, event: QaEvent): QaStreamState {
  switch (event.type) {
    case "meta":
      return {
        ...initialQaState,
        answerId: event.answerId,
        model: event.model,
        fromPageId: event.fromPageId,
        sessionId: event.sessionId || null,
      };
    case "summary_ready":
      return { ...state, summarySources: event.sources };
    case "block_open": {
      const blocks = [...state.blocks];
      blocks[event.index] = event.block;
      return { ...state, blocks };
    }
    case "block_delta":
      return { ...state, blocks: appendDeltaToBlock(state.blocks, event.index, event.textAppend) };
    case "block_close":
      return state;
    case "complete":
      return { ...state, done: true };
    case "cancelled":
      return { ...state, cancelled: true, done: true };
    case "error":
      return { ...state, error: event.error, done: true };
    case "heartbeat":
      // Transport keep-alive — ignored by the UI reducer.
      return state;
    default:
      // Unknown / internal event types (e.g. the hypervisor's ``access``
      // provenance events) are tolerated and ignored — never rendered,
      // never crash the reducer. The SSE parser yields every non-heartbeat
      // frame, so this guard keeps state intact for types outside QaEvent.
      return state;
  }
}

function appendDeltaToBlock(blocks: Block[], index: number, chunk: string): Block[] {
  const target = blocks[index];
  if (!target) return blocks;
  const updated: Block[] = [...blocks];
  switch (target.kind) {
    case "p": {
      const cur = typeof target.text === "string" ? target.text : "";
      updated[index] = { kind: "p", text: cur + chunk };
      break;
    }
    case "h2":
      updated[index] = { kind: "h2", id: target.id, text: target.text + chunk };
      break;
    case "h3":
      updated[index] = { kind: "h3", id: target.id, text: target.text + chunk };
      break;
    case "ul": {
      // For lists, split incoming chunk on `\n` to advance to the next item.
      const segments = chunk.split("\n");
      const items = [...target.items];
      let cursorIdx = Math.max(0, items.length - 1);
      for (let s = 0; s < segments.length; s++) {
        const seg = segments[s];
        if (s > 0) {
          cursorIdx = items.length;
          items.push("");
        }
        const cur = items[cursorIdx];
        const curStr = typeof cur === "string" ? cur : "";
        items[cursorIdx] = curStr + seg;
      }
      updated[index] = { kind: "ul", items };
      break;
    }
    default:
      break;
  }
  return updated;
}

/**
 * Consume the QA event stream into folded UI state. Starts a new stream
 * each time `input` changes; aborts the previous one cleanly. The
 * resulting state mirrors what a snapshot `getAnswer()` would return —
 * UI components can render either source.
 *
 * Each invocation represents exactly ONE in-flight turn: a follow-up carries
 * the SAME ``answerId`` plus a new ``question``, which changes ``key`` and so
 * (re)opens a fresh stream against the continued conversation. Turn
 * accumulation across follow-ups is the caller's job (``QAScreen``), not this
 * hook's — the reducer fully resets on each ``meta``.
 */
export function useQaStream(input: {
  question: string;
  fromPageId: string;
  model: string;
  slug: string;
  /** Continue an existing conversation (a follow-up) instead of minting a new
   *  answer. Absent for the first turn. */
  answerId?: string;
  /** Q&A agent shape for a NEW conversation (server default ``fast`` when
   *  omitted); ignored by the backend on a continuation. */
  mode?: QaMode;
} | null) {
  const [state, dispatch] = useReducer(reduceQa, initialQaState);
  const key = input
    ? `${input.slug}|${input.fromPageId}|${input.model}|${input.answerId ?? ""}|${input.mode ?? ""}|${input.question}`
    : null;
  // The key that produced the folded `state`. Advanced to the live key only
  // once that stream's first event lands. Until then a just-swapped input reads
  // as "not yet settled", so the consumer can paint a skeleton instead of
  // flashing the PREVIOUS turn's blocks under the new question (the reducer
  // only clears on `meta`, which arrives a round-trip later).
  const settledKeyRef = useRef<string | null>(key);

  useEffect(() => {
    if (!input) return;
    const ctrl = new AbortController();
    let cancelled = false;
    (async () => {
      try {
        for await (const event of streamAnswer(input, { signal: ctrl.signal })) {
          if (cancelled) break;
          settledKeyRef.current = key;
          dispatch(event);
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
          settledKeyRef.current = key;
          dispatch({ type: "error", error: toWikiError(err) });
        }
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
    // The key reduces the input dependency to a stable string.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return { ...state, settled: settledKeyRef.current === key };
}

// ── Helpers ─────────────────────────────────────────────────────────

function toWikiError(err: unknown): WikiError {
  if (err && typeof err === "object" && "code" in (err as object)) {
    const x = err as WikiError;
    return { code: x.code, message: x.message, hint: x.hint, fields: x.fields };
  }
  return { code: "internal", message: err instanceof Error ? err.message : String(err) };
}
