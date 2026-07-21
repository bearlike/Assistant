import { useEffect, useMemo, useRef, useState } from 'react';
import type { EventRecord } from '../types';
import { getActiveStreamText, getActiveTurn } from '../utils/timeline';

/** No new events for this long (ms) ⇒ the run reads as stalled. */
const STALL_MS = 15_000;
/** Rough chars-per-token estimate for the live output-rate readout. */
const CHARS_PER_TOKEN = 4;

export interface Throughput {
  /** Smoothed, estimated output tokens/sec. 0 when not streaming. */
  tokPerSec: number;
  /** Coarse activity label, or undefined to let the caller default. */
  phase?: string;
}

/** Tail-of-feed signals a phase is classified from (recomputed on new events). */
interface PhaseSignal {
  lastType?: string;
  lastMs: number;
}

/**
 * Classify the run phase from the event-tail signal + the wall clock. Called
 * every render (cheap) so the stalled check re-evaluates against `Date.now()`
 * without a memo that lint can't model (the seconds tick forces the render).
 */
function classifyPhase(running: boolean, signal: PhaseSignal | null): string | undefined {
  if (!running || !signal) return undefined;
  const quietMs = signal.lastMs ? Date.now() - signal.lastMs : 0;
  if (quietMs > STALL_MS) return 'Stalled';
  switch (signal.lastType) {
    case 'agent_message_delta':
      return 'Streaming';
    case 'llm_call_start':
      return 'Reasoning';
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
 * tok/s rate, and classifies the run phase from the tail of the polled event
 * feed — mirroring the CLI's status semantics: streaming while deltas
 * land, thinking on an open LLM call, tool work between calls, stalled after a
 * quiet spell.
 *
 * NOT a parallel readout — its output feeds the single `RunStatus` that
 * `RunTelemetry` already renders. Reuses `getActiveStreamText` so the token
 * proxy is exactly the text the conversation streams; the poll is the only
 * network source (a 1s tick just re-samples the rate + re-evaluates staleness
 * against the clock, it does not fetch).
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
    for (const ev of turn.events) {
      if (ev.type === 'context' || ev.type === 'title_update') continue;
      lastType = ev.type;
      const ms = Date.parse(ev.ts);
      if (Number.isFinite(ms)) lastMs = ms;
    }
    return { lastType, lastMs };
  }, [events]);

  const outRef = useRef(0);
  outRef.current = outTokens;
  const sampleRef = useRef<{ tokens: number; t: number } | null>(null);
  const [rate, setRate] = useState(0);
  // A once-per-second heartbeat: forces a re-render so the rate decays and the
  // stalled check re-evaluates even when the poll returns no new events.
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
        // EMA smoothing so the readout doesn't jitter poll-to-poll.
        setRate((r) => (r === 0 ? inst : r * 0.5 + inst * 0.5));
      }
      sampleRef.current = { tokens: outRef.current, t: now };
      setTick((n) => n + 1);
    }, 1000);
    return () => clearInterval(id);
  }, [running]);

  return { tokPerSec: Math.round(rate), phase: classifyPhase(running, signal) };
}
