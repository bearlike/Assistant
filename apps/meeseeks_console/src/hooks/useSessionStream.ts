import { useEffect, useRef } from "react";
import { EventRecord } from "../types";

const API_BASE =
  import.meta.env.VITE_API_USE_PROXY === "1" || import.meta.env.VITE_API_USE_PROXY === "true"
    ? ""
    : import.meta.env.VITE_API_BASE_URL || import.meta.env.VITE_API_BASE || "";
const API_KEY = import.meta.env.VITE_API_KEY || "";

/**
 * Subscribe to real-time session events via SSE.
 *
 * Opens an EventSource connection to `/api/sessions/{id}/stream` and
 * calls `onEvent` for each new event. Closes automatically on
 * `stream_end` or when the component unmounts.
 */
export function useSessionStream(
  sessionId: string | null,
  onEvent: (event: EventRecord) => void
) {
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!sessionId) return;

    const params = new URLSearchParams();
    if (API_KEY) params.set("api_key", API_KEY);
    const url = `${API_BASE}/api/sessions/${sessionId}/stream?${params}`;
    const source = new EventSource(url);

    source.onmessage = (e) => {
      try {
        const event = JSON.parse(e.data) as EventRecord & { type: string };
        if (event.type === "stream_end") {
          source.close();
          return;
        }
        onEventRef.current(event);
      } catch {
        // Ignore malformed frames
      }
    };

    source.onerror = () => {
      source.close();
    };

    return () => {
      source.close();
    };
  }, [sessionId]);
}
