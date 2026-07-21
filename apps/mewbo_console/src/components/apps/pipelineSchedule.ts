/**
 * Humane liveness text for one `PipelineLiveness` row.
 * Kept React-free (no JSX) so it stays unit-testable in isolation, same
 * convention as `triggers/triggerFormat.ts`.
 *
 * Deliberately not a cron library — this covers the common shapes an agent
 * actually emits ("every N minutes", "every N hours", hourly, daily) and
 * falls back to the raw expression for anything else (day-of-month/weekday
 * fields, non-numeric minute/hour) rather than guessing wrong.
 */
import { RelativeTime } from "../../utils/relativeTime";
import type { PipelineLiveness } from "../../api/apps";

function everyN(field: string): number | null {
  const m = /^\*\/(\d+)$/.exec(field);
  return m ? Number(m[1]) : null;
}

/** Best-effort cron → plain language. Returns null when the shape isn't one
 * of the common ones, so the caller can fall back to the raw expression. */
function describeCron(cron: string): string | null {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute, hour, dom, month, dow] = parts;
  if (dom !== "*" || month !== "*" || dow !== "*") return null;

  const everyMinute = everyN(minute);
  if (everyMinute != null && hour === "*") {
    return everyMinute === 1 ? "every minute" : `every ${everyMinute} min`;
  }
  const everyHour = everyN(hour);
  if (/^\d+$/.test(minute) && everyHour != null) {
    return everyHour === 1 ? "hourly" : `every ${everyHour} hours`;
  }
  if (/^\d+$/.test(minute) && hour === "*") return "hourly";
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) return "daily";
  return null;
}

/**
 * Liveness text + whether the row should read as a warning, for one pipeline.
 * A declared schedule that isn't armed still warns (that's the actual bug —
 * a schedule was declared but nothing is running it);
 * `on_demand` is a deliberate design choice, never a warning; no schedule and
 * not on-demand keeps the wave-1 "No refresh schedule" warning.
 */
export function describePipelineLiveness(
  pipeline: Pick<PipelineLiveness, "schedule" | "on_demand" | "armed">,
): { text: string; warn: boolean } {
  const { schedule, on_demand, armed } = pipeline;

  if (schedule?.kind === "time.cron") {
    const phrase = describeCron(schedule.cron);
    const base = phrase ? `Refreshes ${phrase}` : schedule.cron;
    return armed ? { text: base, warn: false } : { text: `${base} · not armed`, warn: true };
  }
  if (schedule?.kind === "time.at") {
    const base = `Runs ${RelativeTime.format(schedule.at)}`;
    return armed ? { text: base, warn: false } : { text: `${base} · not armed`, warn: true };
  }
  if (on_demand) return { text: "On demand", warn: false };
  return { text: "No refresh schedule", warn: true };
}
