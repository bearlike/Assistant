import type { TimelineEntry, TriggerTranscriptMeta } from "../../types";
import { coalesceAdjacent } from "../../utils/coalesceAdjacent";

/**
 * Display-layer grouping for repeated trigger transcript rows — a `time.cron`
 * that fires every minute would otherwise stack one "Cron schedule fired" row
 * per fire and turn the transcript into a scroll-heavy wall. This is
 * deliberately NOT done in `utils/timeline.ts::buildTimeline`: that parser is
 * a wire-shape mirror of the parity-tested Python port
 * (`apps/mewbo_mcp/src/mewbo_mcp/timeline.py`), and grouping is a
 * presentation concern, not a parse one — folding it into `buildTimeline`
 * would silently diverge the two implementations.
 *
 * The grouping mechanics themselves live in the generic `coalesceAdjacent`
 * (`utils/coalesceAdjacent.ts`) — this file supplies only the
 * trigger-specific identity key and merge function. Any other row type that
 * turns out to repeat adjacently with identical identity (a chain of LLM
 * retries, …) adopts the same primitive with its own key/merge pair rather
 * than a second bespoke grouping loop.
 */

/**
 * Identity for a trigger transcript entry: same trigger id when the payload
 * carries one, else same `kind` — in BOTH cases also the same `action`.
 * `action` has to be part of the key even when an id is present: an
 * armed-then-fired pair for the same trigger id are two different sentences
 * ("Armed X" vs "X fired"), and merging across them would produce a row
 * whose count lies about what happened. Non-trigger entries return `null`
 * (never merge — `coalesceAdjacent`'s hard-boundary signal). `trigger_armed`
 * repeats fold under this same key too — a paused trigger re-armed several
 * times in a row groups exactly like repeated fires, no separate handling.
 */
function triggerKey(entry: TimelineEntry): string | null {
  if (entry.role !== "trigger" || !entry.trigger) return null;
  const subject = entry.trigger.triggerId
    ? `id:${entry.trigger.triggerId}`
    : `kind:${entry.trigger.kind}`;
  return `${subject}|action:${entry.trigger.action}`;
}

/**
 * Merge a run of same-identity trigger entries into one. Keeps the group's
 * first entry `id` (stable React key across re-renders as a run grows) and
 * `turnId`, but takes the LAST entry's `ts` and `trigger` fields (summary
 * included) — the row reads as "here's the latest, and how many" rather than
 * a stale first snapshot. `firstTs` records the group's opening timestamp for
 * a "first at…" tooltip.
 */
function mergeTriggerGroup(group: TimelineEntry[]): TimelineEntry {
  const first = group[0];
  const last = group[group.length - 1];
  const lastTrigger = last.trigger as TriggerTranscriptMeta;
  return {
    ...first,
    ts: last.ts ?? first.ts,
    trigger: {
      ...lastTrigger,
      count: group.length,
      firstTs: first.ts,
    },
  };
}

/**
 * Fold adjacent `role: "trigger"` entries sharing a {@link triggerKey} into
 * one entry carrying `trigger.count`. Non-trigger entries, and trigger
 * entries with no adjacent match, pass through unchanged (by reference) —
 * `count` stays absent, so a lone occurrence renders exactly as it did before
 * this helper existed.
 */
export function coalesceAdjacentTriggers(entries: TimelineEntry[]): TimelineEntry[] {
  return coalesceAdjacent(entries, triggerKey, mergeTriggerGroup);
}
