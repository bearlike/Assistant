import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { fetchEvents } from "../api/client";
import { EventRecord } from "../types";
import { logApiError } from "../utils/errors";

const POLL_INTERVAL_MS = 1000;

type State = {
  events: EventRecord[];
  running: boolean;
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
      return { events: nextEvents, running: payload.running, lastTs: nextLastTs };
    },
    refetchInterval: (query) => {
      const data = query.state.data;
      // Stop polling once the session is no longer running. `resume()` flips
      // the cache back into a "needs refetch" state to wake polling up.
      return data?.running ? POLL_INTERVAL_MS : false;
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
    error: q.error ? logApiError("fetchEvents", q.error) : null,
    pollingEnabled: q.data?.running ?? true,
    reset,
    resume,
  };
}
