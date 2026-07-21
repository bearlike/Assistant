import { useEffect, useState } from 'react';
import { formatDuration } from '../utils/time';

/**
 * Format elapsed wall-clock since `startTs` via the shared `formatDuration`
 * (`Nms` / `Ns` / `Mm Ss` / `Hh Mm`). Re-renders every second while active.
 * Returns undefined when no timestamp is available so callers can decide
 * whether to render anything.
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
  return formatDuration(now - startMs);
}

/**
 * Elapsed wall-clock in milliseconds since `startMs`. Ticks ~4×/s while
 * `active` so sub-second readouts (e.g. "3.2s · streaming") animate, then
 * freezes once `active` is false. Returns 0 when no start time is set.
 *
 * Pure UI animation clock — not server polling. Stops cleanly on unmount.
 */
export function useElapsedMs(startMs: number | null, active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active || startMs == null) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [active, startMs]);
  if (startMs == null) return 0;
  return Math.max(0, now - startMs);
}
