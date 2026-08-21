/**
 * useThroughput — an outstanding tool call is positive evidence of activity.
 *
 * The engine emits `tool_call` before dispatch and `tool_result` after the
 * tool returns, with nothing in between — a `wiki_build_graph` run can sit
 * quiet for 15+ minutes while it does real work. `classifyPhase` used to
 * measure liveness purely off event recency, so any tool slower than
 * `STALL_MS` read as "Stalled" next to a pulsing live dot. These pin the fix:
 * a still-open `tool_call` suppresses the stall read, and the read reverts
 * once its matching `tool_result` lands and quiet time resumes.
 */
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useThroughput } from '../hooks/useThroughput';
import type { EventRecord } from '../types';

const T0 = '2026-01-01T00:00:00.000Z';

function evt(ts: string, type: string, payload: Record<string, unknown> = {}): EventRecord {
  return { ts, type, payload };
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date(T0));
});

describe('useThroughput — outstanding tool calls override the stall read', () => {
  it('a tool_call with no matching tool_result reads as Running tool, not Stalled, past 15s', () => {
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'tool_call', { tool_call_id: 'call_1', tool_id: 'wiki_build_graph' }),
    ];
    const { result, rerender } = renderHook(() => useThroughput(events, true));

    act(() => {
      vi.setSystemTime(new Date(Date.parse(T0) + 20_000));
    });
    rerender();

    expect(result.current.phase).toBe('Running tool');
  });

  it('once the matching tool_result arrives, a further 15s of silence DOES stall', () => {
    const resultTs = new Date(Date.parse(T0) + 5_000).toISOString();
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'tool_call', { tool_call_id: 'call_1', tool_id: 'wiki_build_graph' }),
      evt(resultTs, 'tool_result', { tool_call_id: 'call_1', tool_id: 'wiki_build_graph' }),
    ];
    const { result, rerender } = renderHook(() => useThroughput(events, true));

    act(() => {
      vi.setSystemTime(new Date(Date.parse(resultTs) + 20_000));
    });
    rerender();

    expect(result.current.phase).toBe('Stalled');
  });

  it('two concurrent tool calls: one resolved, one still outstanding, stays Running tool', () => {
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'tool_call', { tool_call_id: 'call_a', tool_id: 'shell' }),
      evt(T0, 'tool_call', { tool_call_id: 'call_b', tool_id: 'wiki_build_graph' }),
      evt(T0, 'tool_result', { tool_call_id: 'call_a', tool_id: 'shell' }),
    ];
    const { result, rerender } = renderHook(() => useThroughput(events, true));

    act(() => {
      vi.setSystemTime(new Date(Date.parse(T0) + 20_000));
    });
    rerender();

    expect(result.current.phase).toBe('Running tool');
  });
});

describe('useThroughput — stepStartTs (the active row chip clock)', () => {
  it('is the root llm_call_start ts while that call is open (Reasoning)', () => {
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'llm_call_start', { agent_id: 'root', depth: 0, step: 1 }),
    ];
    const { result } = renderHook(() => useThroughput(events, true));
    expect(result.current.stepStartTs).toBe(T0);
  });

  it('is NOT cleared by the matching llm_call_end — it spans the tool phase too', () => {
    const endTs = new Date(Date.parse(T0) + 1_000).toISOString();
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'llm_call_start', { agent_id: 'root', depth: 0, step: 1 }),
      evt(endTs, 'llm_call_end', { agent_id: 'root', depth: 0, step: 1, success: true }),
      evt(endTs, 'tool_call', { tool_call_id: 'call_a', tool_id: 'shell' }),
    ];
    const { result } = renderHook(() => useThroughput(events, true));
    expect(result.current.phase).toBe('Running tool');
    // Elapsed still measures from the ORIGINAL call start, not the tool
    // dispatch — the step clock resets only at the next root call.
    expect(result.current.stepStartTs).toBe(T0);
  });

  it('resets to the NEXT root llm_call_start once the step advances', () => {
    const t1 = new Date(Date.parse(T0) + 1_000).toISOString();
    const t2 = new Date(Date.parse(T0) + 2_000).toISOString();
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'llm_call_start', { agent_id: 'root', depth: 0, step: 1 }),
      evt(t1, 'llm_call_end', { agent_id: 'root', depth: 0, step: 1, success: true }),
      evt(t2, 'llm_call_start', { agent_id: 'root', depth: 0, step: 2 }),
    ];
    const { result } = renderHook(() => useThroughput(events, true));
    expect(result.current.stepStartTs).toBe(t2);
  });

  it('a sub-agent (depth > 0) llm_call_start never sets the root step clock', () => {
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'llm_call_start', { agent_id: 'sub-1', depth: 1, step: 1 }),
    ];
    const { result } = renderHook(() => useThroughput(events, true));
    expect(result.current.stepStartTs).toBeUndefined();
  });

  it('is undefined once the run is not `running`', () => {
    const events: EventRecord[] = [
      evt(T0, 'user', {}),
      evt(T0, 'llm_call_start', { agent_id: 'root', depth: 0, step: 1 }),
    ];
    const { result } = renderHook(() => useThroughput(events, false));
    expect(result.current.stepStartTs).toBeUndefined();
  });
});
