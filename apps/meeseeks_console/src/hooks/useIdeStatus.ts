import { useCallback, useEffect, useState } from "react";
import { getIde, IdeInstance } from "../api/ide";

const POLL_INTERVAL_MS = 30_000;

function sameInstance(a: IdeInstance | null, b: IdeInstance | null): boolean {
  if (a === b) return true;
  if (!a || !b) return false;
  return (
    a.status === b.status &&
    a.expires_at === b.expires_at &&
    a.extensions === b.extensions
  );
}

/**
 * Poll `GET /api/sessions/<sid>/ide` every 30s while the page is visible AND
 * while there is actually an instance to watch. After a `null` response the
 * loop stops; the user refreshing the page (or calling `refresh()`) is the
 * only way to pick up an instance created in another tab.
 */
export function useIdeStatus(sessionId: string | null | undefined) {
  const [instance, setInstance] = useState<IdeInstance | null>(null);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      const inst = await getIde(sessionId);
      setInstance((prev) => (sameInstance(prev, inst) ? prev : inst));
    } catch {
      // Swallow — the session page keeps rendering whatever state it has.
    }
  }, [sessionId]);

  useEffect(() => {
    if (!sessionId) {
      setInstance(null);
      return undefined;
    }

    // Per-effect liveness flag — prevents an in-flight response from an
    // old sessionId from writing into the new session's state.
    let active = true;
    let timer: number | null = null;
    const controller = new AbortController();
    let lastSeen: IdeInstance | null = null;

    const clearTimer = () => {
      if (timer !== null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    const fetchOnce = async () => {
      try {
        const inst = await getIde(sessionId, controller.signal);
        if (!active) return;
        lastSeen = inst;
        setInstance((prev) => (sameInstance(prev, inst) ? prev : inst));
      } catch (err) {
        if (!active) return;
        if (err instanceof DOMException && err.name === "AbortError") return;
        // Transient failure — keep the loop going, next tick will retry.
      }
    };

    const schedule = () => {
      clearTimer();
      // No point re-arming when: tab is hidden, or we've confirmed there is
      // no instance yet (user hasn't opened the IDE for this session).
      if (document.visibilityState !== "visible") return;
      if (lastSeen === null) return;
      timer = window.setTimeout(async () => {
        timer = null;
        if (!active) return;
        await fetchOnce();
        if (active) schedule();
      }, POLL_INTERVAL_MS);
    };

    const onVisibility = () => {
      if (!active) return;
      if (document.visibilityState === "visible") {
        void fetchOnce().then(() => {
          if (active) schedule();
        });
      } else {
        clearTimer();
      }
    };

    void fetchOnce().then(() => {
      if (active) schedule();
    });
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      active = false;
      controller.abort();
      clearTimer();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [sessionId]);

  return { instance, refresh, setInstance };
}
