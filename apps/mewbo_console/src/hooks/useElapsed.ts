import { useEffect, useState } from 'react';

/**
 * Format elapsed wall-clock since `startTs` as `Hh Mm` / `Mm Ss` / `Ss`.
 * Re-renders every second while active. Returns undefined when no
 * timestamp is available so callers can decide whether to render anything.
 *
 * Used by both the composer's running-state strip and the workspace
 * FlowerSpinner — same data, two windows on one truth.
 */
export function useElapsed(startTs?: string, active?: boolean): string | undefined {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || !startTs) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active, startTs]);
  if (!startTs) return undefined;
  const startMs = Date.parse(startTs);
  if (!Number.isFinite(startMs)) return undefined;
  const totalSec = Math.max(0, Math.floor((now - startMs) / 1000));
  const m = Math.floor(totalSec / 60);
  const s = totalSec % 60;
  if (m >= 60) {
    const h = Math.floor(m / 60);
    return `${h}h ${m % 60}m`;
  }
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}
