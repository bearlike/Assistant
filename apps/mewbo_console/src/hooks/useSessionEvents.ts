import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { fetchEvents } from "../api/client";
import { EventRecord } from "../types";
import { logApiError } from "../utils/errors";

const POLL_INTERVAL_MS = 1000;
// Kept alive (never `false`) once a run finishes so an off-tab start (CLI,
// Aura, a trigger, or a plan/question dead-end) still surfaces here without
// a manual refresh — just at a slower cadence than the in-flight poll.
const KEEPALIVE_INTERVAL_MS = 15000;

type State = {
  events: EventRecord[];
  running: boolean;
  status?: string;
  doneReason?: string;
  terminated?: boolean;
  recoverable?: boolean;
  lastTs?: string;
};

function eventKey(event: EventRecord): string {
  const payload =
    event.payload && typeof event.payload === "object"
      ? JSON.stringify(event.payload)
      : String(event.payload ?? "");
  return `${event.ts}|${event.type}|${payload}`;
}

export function useSessionEvents(sessionId?: string) {
  const qc = useQueryClient();
  const lastTsRef = useRef<string | undefined>(undefined);

  const key = ["session-events", sessionId ?? ""] as const;

  // Reset cursor whenever the session changes — the cached state is keyed
  // by sessionId so the previous session's data stays in its own slot.
  useEffect(() => {
    lastTsRef.current = undefined;
  }, [sessionId]);

  const q = useQuery<State>({
    queryKey: key,
    enabled: Boolean(sessionId),
    queryFn: async () => {
      if (!sessionId) {
        return { events: [], running: false, lastTs: undefined } as State;
      }
      const prev =
        qc.getQueryData<State>(key) ??
        ({ events: [], running: false, lastTs: undefined } as State);
      const payload = await fetchEvents(sessionId, lastTsRef.current);
      let nextEvents = prev.events;
      let nextLastTs = prev.lastTs;
      if (payload.events.length) {
        const seen = new Set(prev.events.map(eventKey));
        const fresh = payload.events.filter((e) => !seen.has(eventKey(e)));
        if (fresh.length) {
          nextEvents = [...prev.events, ...fresh];
        }
        nextLastTs = payload.events[payload.events.length - 1].ts;
        lastTsRef.current = nextLastTs;
      }
      return {
        events: nextEvents,
        running: payload.running,
        status: payload.status,
        doneReason: payload.done_reason,
        terminated: payload.terminated,
        recoverable: payload.recoverable,
        lastTs: nextLastTs,
      };
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      // Termination is an absorbing state (SessionDetailView's `isTerminated`
      // — the runtime ships no un-terminate primitive), so a terminated
      // session can never start running again; polling it forever would be
      // pure waste. Checked strictly `=== true` — `terminated` is undefined
      // before the first poll returns, and that must fall through to the
      // keepalive tick below, never stop a session that may still be live.
      if (data?.terminated === true) return false;
      // Slow to a keepalive tick rather than stopping outright once the
      // session is no longer running — `resume()` alone can't catch an
      // off-tab start (CLI/Aura/trigger), so this is what makes one
      // observable without a manual refresh.
      return data?.running ? POLL_INTERVAL_MS : KEEPALIVE_INTERVAL_MS;
    },
    staleTime: 0,
  });

  const reset = useCallback(() => {
    lastTsRef.current = undefined;
    qc.setQueryData<State>(key, {
      events: [],
      running: false,
      lastTs: undefined,
    });
    void qc.invalidateQueries({ queryKey: key });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qc, sessionId]);

  const resume = useCallback(() => {
    if (!sessionId) return;
    void qc.invalidateQueries({ queryKey: key });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qc, sessionId]);

  return {
    events: q.data?.events ?? [],
    running: q.data?.running ?? false,
    status: q.data?.status,
    doneReason: q.data?.doneReason,
    terminated: q.data?.terminated,
    recoverable: q.data?.recoverable,
    error: q.error ? logApiError("fetchEvents", q.error) : null,
    pollingEnabled: q.data?.running ?? true,
    reset,
    resume,
  };
}
