/**
 * RelativeTime — atomic helper for "X minutes ago" formatting.
 *
 * Canonical zero-dependency formatting via the browser's
 * ``Intl.RelativeTimeFormat`` API. Exposed as a class with static methods
 * so callers can either consume the canonical output via ``format(iso)``
 * or build their own variant by composing the lower-level helpers.
 */

interface Unit {
  unit: Intl.RelativeTimeFormatUnit;
  seconds: number;
}

// Largest-first ordering — ``format`` walks this until the diff fits.
const UNITS: readonly Unit[] = [
  { unit: "year", seconds: 365 * 24 * 60 * 60 },
  { unit: "month", seconds: 30 * 24 * 60 * 60 },
  { unit: "week", seconds: 7 * 24 * 60 * 60 },
  { unit: "day", seconds: 24 * 60 * 60 },
  { unit: "hour", seconds: 60 * 60 },
  { unit: "minute", seconds: 60 },
  { unit: "second", seconds: 1 },
];

const FMT = typeof Intl !== "undefined" && "RelativeTimeFormat" in Intl
  ? new Intl.RelativeTimeFormat("en", { numeric: "auto" })
  : null;

export class RelativeTime {
  // ── Pure static behaviour ───────────────────────────────────────────

  /** Parse *iso* to epoch seconds. Returns NaN if the string is bad. */
  static parse(iso: string | null | undefined): number {
    if (!iso) return Number.NaN;
    const t = Date.parse(iso);
    return Number.isFinite(t) ? t / 1000 : Number.NaN;
  }

  /**
   * Render *iso* as "5 minutes ago" / "in 3 hours". Falls back to the raw
   * string when ``Intl.RelativeTimeFormat`` is unavailable or *iso* fails
   * to parse — never throws, so callers can drop it inline.
   */
  static format(iso: string | null | undefined, now = Date.now() / 1000): string {
    const epoch = RelativeTime.parse(iso);
    if (Number.isNaN(epoch)) return iso ?? "";
    if (!FMT) return iso ?? "";
    const diff = epoch - now; // negative = past
    const abs = Math.abs(diff);
    for (const { unit, seconds } of UNITS) {
      if (abs >= seconds || unit === "second") {
        const value = Math.round(diff / seconds);
        return FMT.format(value, unit);
      }
    }
    return FMT.format(Math.round(diff), "second");
  }

  /** Absolute ISO timestamp suitable for tooltips ("2026-05-14 23:22 UTC"). */
  static tooltip(iso: string | null | undefined): string {
    if (!iso) return "";
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return iso;
    const d = new Date(t);
    const pad = (n: number) => String(n).padStart(2, "0");
    return (
      `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ` +
      `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
    );
  }
}
