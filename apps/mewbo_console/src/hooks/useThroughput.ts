import { useEffect, useMemo, useRef, useState } from 'react';
import type { EventRecord } from '../types';
import { getActiveStreamText, getActiveTurn } from '../utils/timeline';
import { readNumber } from '../utils/payload';

/** No new events for this long (ms) ⇒ the run reads as stalled. */
const STALL_MS = 15_000;
/** Rough chars-per-token estimate for the live output-rate readout. */
const CHARS_PER_TOKEN = 4;

export interface Throughput {
  /** Smoothed, estimated output tokens/sec. 0 when not streaming. */
  tokPerSec: number;
  /** Coarse activity label, or undefined to let the caller default. */
  phase?: string;
  /**
   * `ts` of the active turn's current STEP — the most recent root (depth 0)
   * `llm_call_start`. Deliberately never cleared by that call's matching
   * `llm_call_end`: a step is "the call plus whatever tool dispatch follows
   * it", so the clock keeps running through the `Running tool` phase and
   * only resets when the NEXT root call starts. Undefined once the turn has
   * closed (no open turn) or before the first root call has started.
   */
  stepStartTs?: string;
}

/** Tail-of-feed signals a phase is classified from (recomputed on new events). */
interface PhaseSignal {
  lastType?: string;
  lastMs: number;
  /**
   * A dispatched `tool_call` whose `tool_call_id` has no matching `tool_result`
   * yet. The engine emits nothing between the two, so a tool slower than
   * `STALL_MS` would otherwise read as dead — this is the positive evidence
   * that it is still working, not quiet in a way that means trouble.
   */
  toolCallOutstanding: boolean;
  /** See {@link Throughput.stepStartTs}. */
  stepStartTs?: string;
}

/**
 * Classify the run phase from the event-tail signal + the wall clock. Called
 * every render (cheap) so the stalled check re-evaluates against `Date.now()`
 * without a memo that lint can't model (the seconds tick forces the render).
 */
function classifyPhase(running: boolean, signal: PhaseSignal | null): string | undefined {
  if (!running || !signal) return undefined;
  // An in-flight tool call is checked before the quiet-time gate: silence
  // while a tool is dispatched is expected, not evidence of a stalled run.
  if (signal.toolCallOutstanding) return 'Running tool';
  const quietMs = signal.lastMs ? Date.now() - signal.lastMs : 0;
  if (quietMs > STALL_MS) return 'Stalled';
  switch (signal.lastType) {
    case 'agent_message_delta':
      return 'Streaming';
    case 'llm_call_start':
      return 'Reasoning';
    case 'tool_call':
    case 'llm_call_end':
    case 'tool_result':
      return 'Running tool';
    default:
      return undefined;
  }
}

/**
 * Live token-throughput + phase readout for the run telemetry.
 * Differences the in-flight turn's streamed output over wall-clock into a
 * tok/s rate, and classifies the run phase from the tail of the streamed event
 * feed — mirroring the CLI's status semantics: streaming while deltas
 * land, thinking on an open LLM call, tool work between calls, stalled after a
 * quiet spell.
 *
 * NOT a parallel readout — its output feeds the single `RunStatus` that
 * `RunTelemetry` already renders. Reuses `getActiveStreamText` so the token
 * proxy is exactly the text the conversation streams; the session stream is
 * the only network source (a 1s tick just re-samples the rate + re-evaluates
 * staleness against the clock, it does not fetch).
 */
export function useThroughput(events: EventRecord[], running: boolean): Throughput {
  // Estimated cumulative output tokens for the in-flight turn (root narration).
  const outTokens = useMemo(
    () => Math.round(getActiveStreamText(events).length / CHARS_PER_TOKEN),
    [events],
  );
  // Event-tail signals for phase classification (recomputed only on new events).
  const signal = useMemo<PhaseSignal | null>(() => {
    const turn = getActiveTurn(events);
    if (!turn) return null;
    let lastType: string | undefined;
    let lastMs = 0;
    // A turn can dispatch several tool calls concurrently, so track each by
    // its `tool_call_id` rather than a single flag — one settling must not
    // clear the "still running" read for a sibling call still in flight.
    const outstandingToolCalls = new Set<string>();
    let stepStartTs: string | undefined;
    for (const ev of turn.events) {
      if (ev.type === 'context' || ev.type === 'title_update') continue;
      lastType = ev.type;
      const ms = Date.parse(ev.ts);
      if (Number.isFinite(ms)) lastMs = ms;
      const callId = ev.payload?.tool_call_id;
      if (typeof callId === 'string' && callId) {
        if (ev.type === 'tool_call') outstandingToolCalls.add(callId);
        else if (ev.type === 'tool_result') outstandingToolCalls.delete(callId);
      }
      // Root call only — a sub-agent's own call cycle says nothing about how
      // long THIS step (the one the active row renders) has been running.
      // Never cleared on the matching `llm_call_end`; see `stepStartTs` doc.
      if (ev.type === 'llm_call_start' && (readNumber(ev.payload, 'depth') ?? 0) === 0) {
        stepStartTs = ev.ts;
      }
    }
    return { lastType, lastMs, toolCallOutstanding: outstandingToolCalls.size > 0, stepStartTs };
  }, [events]);

  const outRef = useRef(0);
  outRef.current = outTokens;
  const sampleRef = useRef<{ tokens: number; t: number } | null>(null);
  const [rate, setRate] = useState(0);
  // A once-per-second heartbeat: forces a re-render so the rate decays and the
  // stalled check re-evaluates even when the stream delivers no new events.
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!running) {
      sampleRef.current = null;
      setRate(0);
      return;
    }
    const id = setInterval(() => {
      const now = Date.now();
      const prev = sampleRef.current;
      if (prev && now > prev.t) {
        const dt = (now - prev.t) / 1000;
        const inst = Math.max(0, (outRef.current - prev.tokens) / dt);
        // EMA smoothing so the readout doesn't jitter sample-to-sample.
        setRate((r) => (r === 0 ? inst : r * 0.5 + inst * 0.5));
      }
      sampleRef.current = { tokens: outRef.current, t: now };
      setTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(id);
  }, [running]);

  return {
    tokPerSec: Math.round(rate),
    phase: classifyPhase(running, signal),
    // Only meaningful while a turn is actually open — mirrors `phase`'s own
    // `!running || !signal` gate so a caller never has to re-derive it.
    stepStartTs: running ? signal?.stepStartTs : undefined,
  };
}
