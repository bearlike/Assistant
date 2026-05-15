/**
 * TanStack-Query hooks + a pair of stream consumers that match the
 * transport-level contract the backend will implement (SSE + REST).
 *
 * Streaming hooks (`useIndexingStream`, `useQaStream`) own an
 * `AbortController` per mount, so unmounting or starting a new request
 * cancels the in-flight stream in both the mock and the production
 * transport via the same surface.
 */

import { useEffect, useMemo, useReducer, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  askQuestion,
  cancelIndexingJob,
  createIndexingJob,
  deleteProject,
  getIndexingJob,
  getKnowledgeGraph,
  getPage,
  getWikiDefaults,
  listActiveJobs,
  listLanguages,
  listPlatforms,
  listProjects,
  requestWikiRefresh,
  startAnswer,
  streamAnswer,
  submitWizard,
  subscribeToIndexing,
} from "./client";
import type {
  Block,
  IndexingEvent,
  IndexingJob,
  IndexingLogEntry,
  IndexingPhase,
  QaEvent,
  WikiError,
  WizardSubmission,
} from "./types";

export function useWikiProjects() {
  return useQuery({
    queryKey: ["wiki", "projects"],
    queryFn: listProjects,
    staleTime: 60_000,
  });
}

/**
 * Load the persisted code knowledge graph for *slug*. The graph is a
 * derived, idempotent read — long staleTime so a tab keeps cache between
 * navigations. Disabled when no slug is set (e.g. SSR / unmounted).
 */
/**
 * Load the persisted code knowledge graph for *slug*. Pass ``limit`` to
 * cap the node set (degree-ranked truncation on the BE) — omit it to
 * load the full graph. The BE reports ``stats.totalNodes`` and
 * ``stats.truncated`` so the consumer can surface "showing N of M" when
 * a cap kicks in.
 */
export function useKnowledgeGraph(slug: string | null, limit?: number) {
  return useQuery({
    queryKey: ["wiki", "graph", slug ?? null, limit ?? null],
    queryFn: () =>
      getKnowledgeGraph(slug as string, limit != null ? { limit } : {}),
    enabled: slug != null,
    staleTime: 5 * 60_000,
  });
}

/**
 * Active (non-terminal) indexing jobs. Polls every 4 s so the landing
 * page's "Indexing now" surface stays close-to-live without hammering
 * the API. Pauses automatically when the tab is hidden (TanStack Query
 * default `refetchIntervalInBackground: false`).
 */
export function useActiveIndexingJobs() {
  return useQuery({
    queryKey: ["wiki", "jobs", "active"],
    queryFn: listActiveJobs,
    staleTime: 4_000,
    refetchInterval: 4_000,
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => deleteProject(slug),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

export function useWikiPlatforms() {
  return useQuery({
    queryKey: ["wiki", "platforms"],
    queryFn: listPlatforms,
    staleTime: Infinity,
  });
}

export function useWikiLanguages() {
  return useQuery({
    queryKey: ["wiki", "languages"],
    queryFn: listLanguages,
    staleTime: Infinity,
  });
}

/**
 * Wiki-specific defaults from app.json (``wiki.default_model`` etc.).
 * The wiki picker prefers these over the global /api/models default so
 * operators can pin a fast/cheap/stable model for indexing without
 * affecting the rest of the app. Missing keys fall back to FE defaults.
 */
export function useWikiDefaults() {
  return useQuery({
    queryKey: ["wiki", "defaults"],
    queryFn: getWikiDefaults,
    staleTime: 5 * 60_000,
  });
}

export function useWikiPage(pageId: string | null, slug?: string) {
  return useQuery({
    queryKey: ["wiki", "page", slug ?? null, pageId],
    queryFn: () =>
      pageId && slug ? getPage(slug, pageId) : Promise.resolve(null),
    enabled: Boolean(pageId) && Boolean(slug),
    staleTime: 5 * 60_000,
  });
}

/**
 * Snapshot-only indexing job lookup. Polling fallback for SSE; the
 * function-form `refetchInterval` pauses automatically when the live
 * status reaches a terminal state.
 */
export function useIndexingJob(jobId: string | null) {
  return useQuery({
    queryKey: ["wiki", "indexing", jobId],
    queryFn: () => (jobId ? getIndexingJob(jobId) : Promise.resolve(null)),
    enabled: Boolean(jobId),
    refetchInterval: (q) => {
      const last = q.state.data;
      if (!last) return 500;
      return last.status === "complete" || last.status === "cancelled" || last.status === "failed"
        ? false
        : 500;
    },
  });
}

export function useStartIndexing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { slug: string }) => createIndexingJob(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] }),
  });
}

export function useCancelIndexing() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => cancelIndexingJob(jobId),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] });
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

export function useSubmitWizard() {
  return useMutation({
    mutationFn: (input: WizardSubmission) => submitWizard(input),
  });
}

export function useRequestWikiRefresh() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => requestWikiRefresh(slug),
    onSettled: () => {
      qc.invalidateQueries({ queryKey: ["wiki", "jobs", "active"] });
      qc.invalidateQueries({ queryKey: ["wiki", "projects"] });
    },
  });
}

/** Atomic-class consumers (sidebar caption, landing card) read the
 *  matching Project off the projects list — no per-slug endpoint exists
 *  yet and one is not required for these UI surfaces. */
export function useWikiProjectBySlug(slug: string | undefined) {
  return useQuery({
    queryKey: ["wiki", "projects"],
    queryFn: listProjects,
    staleTime: 60_000,
    select: (projects) => projects.find((p) => p.slug === slug),
    enabled: Boolean(slug),
  });
}

/** Legacy non-streaming QA — preserved for now; new code uses `useQaStream`. */
export function useAskWiki() {
  return useMutation({
    mutationFn: ({
      question,
      fromPageId,
      model,
      slug,
    }: {
      question: string;
      fromPageId: string;
      model: string;
      slug: string;
    }) => askQuestion(question, { fromPageId, model, slug }),
  });
}

export function useStartAnswer() {
  return useMutation({
    mutationFn: startAnswer,
  });
}

// ── Streaming hooks ─────────────────────────────────────────────────

interface IndexingStreamState {
  /** Current job snapshot, folded from incoming events. */
  job: IndexingJob | null;
  /** Rolling list of scan history rows for the UI. */
  history: Array<{ name: string; done: boolean }>;
  /** Current coarse phase from the BE state machine — null until the
   *  first ``phase`` event arrives (legacy backends never emit it). */
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
};

function reduceIndexing(state: IndexingStreamState, event: IndexingEvent): IndexingStreamState {
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
          }
        : state;
    case "cancelled":
      return state.job
        ? { ...state, job: { ...state.job, status: "cancelled", currentFile: null } }
        : state;
    case "error":
      return { ...state, error: event.error };
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
    case "log":
      return {
        ...state,
        logs: [
          ...state.logs,
          { level: event.level, text: event.text, ts: Date.now() / 1000 },
        ],
      };
  }
}

/**
 * Consume the indexing event stream into folded UI state. Handles
 * cancellation via an internal `AbortController` that fires on unmount
 * or when `jobId` changes.
 */
export function useIndexingStream(jobId: string | null) {
  const [state, dispatch] = useReducer(reduceIndexing, initialIndexingState);
  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!jobId) return;
    const ctrl = new AbortController();
    controllerRef.current = ctrl;
    let cancelled = false;
    (async () => {
      try {
        for await (const event of subscribeToIndexing(jobId, { signal: ctrl.signal })) {
          if (cancelled) break;
          dispatch(event);
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
          dispatch({
            type: "error",
            error: toWikiError(err),
          });
        }
      }
    })();
    return () => {
      cancelled = true;
      ctrl.abort();
    };
  }, [jobId]);

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
 */
export function useQaStream(input: {
  question: string;
  fromPageId: string;
  model: string;
  slug: string;
} | null) {
  const [state, dispatch] = useReducer(reduceQa, initialQaState);
  const key = input ? `${input.slug}|${input.fromPageId}|${input.model}|${input.question}` : null;

  useEffect(() => {
    if (!input) return;
    const ctrl = new AbortController();
    let cancelled = false;
    (async () => {
      try {
        for await (const event of streamAnswer(input, { signal: ctrl.signal })) {
          if (cancelled) break;
          dispatch(event);
        }
      } catch (err) {
        if (!ctrl.signal.aborted) {
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

  return state;
}

// ── Helpers ─────────────────────────────────────────────────────────

function toWikiError(err: unknown): WikiError {
  if (err && typeof err === "object" && "code" in (err as object)) {
    const x = err as WikiError;
    return { code: x.code, message: x.message, hint: x.hint, fields: x.fields };
  }
  return { code: "internal", message: err instanceof Error ? err.message : String(err) };
}
