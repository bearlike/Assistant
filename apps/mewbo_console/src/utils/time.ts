/**
 * Compact a token count for display: 1234 → "1234", 5200 → "5.2k",
 * 12_000 → "12k" (trailing ".0" stripped), 1_500_000 → "1.5m". Null-safe:
 * nullish / non-finite / negative → "" so an absent count renders as nothing
 * rather than "NaNk" (adopts the agentic_search `compactTokens` convention this
 * consolidates; the "m" bucket is preserved so large counts stay readable).
 */
export function formatTokens(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1).replace(/\.0$/, "")}m`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1).replace(/\.0$/, "")}k`;
  return String(n);
}

/**
 * Duration formatter, ms in. Floor-based bucketing, two units at most:
 *   <1s → "420ms" · <1min → "42s" · <1hr → "3m 20s" · else → "2h 5m".
 * Every boundary floors (no rounding up, no "60s"), and negatives clamp to 0.
 * This is THE console duration formatter — TerminalCard, the conversation
 * timeline's turn durations, and `useElapsed` all read from it, so a duration
 * reads the same everywhere.
 */
export function formatDuration(ms: number): string {
  const t = Math.max(0, ms);
  if (t < 1_000) return `${Math.floor(t)}ms`;
  if (t < 60_000) return `${Math.floor(t / 1_000)}s`;
  if (t < 3_600_000) return `${Math.floor(t / 60_000)}m ${Math.floor((t % 60_000) / 1_000)}s`;
  return `${Math.floor(t / 3_600_000)}h ${Math.floor((t % 3_600_000) / 60_000)}m`;
}

const DATE_TIME = new Intl.DateTimeFormat(undefined, {
  dateStyle: "medium",
  timeStyle: "short",
});
/**
 * Absolute local date + time for at-rest rows (API keys, git credentials):
 * "Jul 19, 2026, 3:04 PM". Returns the raw string unchanged if it can't be
 * parsed, so a malformed timestamp never throws or renders "Invalid Date".
 */
export function formatDateTime(iso: string): string {
  try {
    return DATE_TIME.format(new Date(iso));
  } catch {
    return iso;
  }
}

const SHORT_DATE = new Intl.DateTimeFormat("en-US", {
  month: "short",
  day: "numeric",
  year: "numeric"
});
export function formatSessionTime(value?: string | null): string {
  if (!value) {
    return "Just now";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  if (diffMs < 0) {
    return SHORT_DATE.format(date);
  }
  const diffSeconds = Math.floor(diffMs / 1000);
  if (diffSeconds < 60) {
    return "just now";
  }
  const diffMinutes = Math.floor(diffSeconds / 60);
  if (diffMinutes < 60) {
    return `${diffMinutes} min ago`;
  }
  const diffHours = Math.floor(diffMinutes / 60);
  if (diffHours < 24) {
    return `${diffHours} hr ago`;
  }
  return SHORT_DATE.format(date);
}
