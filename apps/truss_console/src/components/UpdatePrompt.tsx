import { useRegisterSW } from "virtual:pwa-register/react";
import { Button } from "./ui/button";

// Poll every 15min while the tab is open; also check on tab-focus.
// Workbox compares the SW bytes — if unchanged, no-op. Shorter interval
// means users see the update prompt soon after a deploy instead of up to
// an hour later.
const UPDATE_CHECK_INTERVAL_MS = 15 * 60 * 1000;

async function clearAllCaches() {
  if (!("caches" in window)) return;
  try {
    const keys = await caches.keys();
    await Promise.all(keys.map((k) => caches.delete(k)));
  } catch {
    // No-op: caches API is best-effort here; the hard reload still runs.
  }
}

async function unregisterAllServiceWorkers() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const regs = await navigator.serviceWorker.getRegistrations();
    await Promise.all(regs.map((r) => r.unregister()));
  } catch {
    // No-op: unregister is best-effort; the hard reload still runs.
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
    // Unconditional full reset on every Reload click: unregister every SW,
    // wipe the Cache Storage, then hard-reload. This makes the refresh truly
    // "complete" — no dependence on the skipWaiting/controllerchange handshake,
    // no stale precache surviving into the next page load. Users who miss a
    // deploy never end up in the stale-bundle 404 trap again.
    void Promise.all([unregisterAllServiceWorkers(), clearAllCaches()])
      .finally(() => {
        // updateServiceWorker(true) is redundant now but kept in case the
        // unregister/clear race with the hard reload loses on some browsers.
        void updateServiceWorker(true).catch(() => undefined);
        window.location.reload();
      });
  };

  return (
    <div
      role="status"
      aria-live="polite"
      className="fixed bottom-4 right-4 z-[60] flex items-center gap-3 rounded-lg border border-neutral-700 bg-neutral-900/95 px-4 py-3 text-sm text-neutral-100 shadow-lg backdrop-blur"
    >
      <span>A new version of Truss is available.</span>
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
