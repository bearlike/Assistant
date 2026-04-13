import { useRegisterSW } from "virtual:pwa-register/react";
import { Button } from "./ui/button";

// Poll every 60min while the tab is open; also check on tab-focus.
// Workbox compares the SW bytes — if unchanged, no-op.
const UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000;
// If the SW skip-waiting + controllerchange handshake doesn't reload the
// page within this window, fall back to a manual cache-clear + hard reload.
// Covers: no-waiting-SW (state desync), dev mode (no SW registered),
// private browsing (caches API restricted), or any silent rejection.
const RELOAD_FALLBACK_MS = 1500;

async function clearAllCaches() {
  if (!("caches" in window)) return;
  try {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
  } catch {
    // No-op: caches API is best-effort here; the hard reload still runs.
  }
}

export function UpdatePrompt() {
  const {
    needRefresh: [needRefresh, setNeedRefresh],
    updateServiceWorker
  } = useRegisterSW({
    onRegisteredSW(_url, registration) {
      if (!registration) return;
      const tick = () => {
        if (registration.installing || !navigator) return;
        if ("connection" in navigator && !(navigator as Navigator).onLine) return;
        void registration.update();
      };
      setInterval(tick, UPDATE_CHECK_INTERVAL_MS);
      window.addEventListener("focus", tick);
    }
  });

  if (!needRefresh) return null;

  const handleReload = () => {
    // Happy path: updateServiceWorker(true) reloads the page itself once
    // the new SW takes control. The safety timer below only fires if that
    // handshake silently no-ops — in which case we clear caches and force
    // a hard reload so the user is never stuck on a stale build.
    const safety = window.setTimeout(() => {
      void clearAllCaches().finally(() => window.location.reload());
    }, RELOAD_FALLBACK_MS);
    void updateServiceWorker(true).catch(() => {
      window.clearTimeout(safety);
      void clearAllCaches().finally(() => window.location.reload());
    });
  };

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-[60] flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900/95 px-4 py-3 text-sm text-neutral-100 shadow-lg backdrop-blur"
    >
      <span>A new version of Meeseeks is available.</span>
      <Button
        variant="primary"
        size="sm"
        onClick={handleReload}
      >
        Reload
      </Button>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => setNeedRefresh(false)}
        aria-label="Dismiss update notification"
      >
        Later
      </Button>
    </div>
  );
}
