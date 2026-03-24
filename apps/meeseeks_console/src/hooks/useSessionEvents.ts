import { useCallback, useEffect, useRef, useState } from "react";
import { fetchEvents } from "../api/client";
import { EventRecord } from "../types";
import { logApiError } from "../utils/errors";
const POLL_INTERVAL_MS = 1000;
export function useSessionEvents(sessionId?: string) {
  const [events, setEvents] = useState<EventRecord[]>([]);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pollingEnabled, setPollingEnabled] = useState(true);
  const lastTsRef = useRef<string | undefined>(undefined);
  const hasFetchedRef = useRef(false);
  const reset = useCallback(() => {
    setEvents([]);
    setRunning(false);
    setError(null);
    setPollingEnabled(true);
    lastTsRef.current = undefined;
    hasFetchedRef.current = false;
  }, []);
  const getEventKey = useCallback((event: EventRecord) => {
    const payload =
      event.payload && typeof event.payload === "object"
        ? JSON.stringify(event.payload)
        : String(event.payload ?? "");
    return `${event.ts}|${event.type}|${payload}`;
  }, []);
  useEffect(() => {
    reset();
  }, [sessionId, reset]);
  useEffect(() => {
    if (!sessionId || !pollingEnabled) {
      return;
    }
    let cancelled = false;
    const poll = async () => {
      try {
        const payload = await fetchEvents(sessionId, lastTsRef.current);
        if (cancelled) {
          return;
        }
        hasFetchedRef.current = true;
        setError(null);
        if (payload.events.length) {
          setEvents((prev) => {
            const seen = new Set(prev.map(getEventKey));
            const next = payload.events.filter(
              (event) => !seen.has(getEventKey(event))
            );
            return next.length ? [...prev, ...next] : prev;
          });
          lastTsRef.current = payload.events[payload.events.length - 1].ts;
        }
        setRunning(payload.running);
        if (!payload.running) {
          setPollingEnabled(false);
        }
      } catch (err) {
        if (!cancelled) {
          const message = logApiError("fetchEvents", err);
          setError(message);
          setRunning(false);
          if (hasFetchedRef.current) {
            setPollingEnabled(false);
          }
        }
      }
    };
    const interval = window.setInterval(() => {
      void poll();
    }, POLL_INTERVAL_MS);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [sessionId, pollingEnabled, getEventKey]);
  const resume = useCallback(() => {
    if (!sessionId) {
      return;
    }
    setPollingEnabled(true);
  }, [sessionId]);
  return {
    events,
    running,
    error,
    reset,
    resume,
    pollingEnabled
  };
}
