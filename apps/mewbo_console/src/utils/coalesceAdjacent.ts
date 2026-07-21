/**
 * Generic "fold adjacent same-identity items into one" primitive. Built for
 * the trigger-transcript coalescing (`components/triggers/coalesceTriggers.ts`)
 * but deliberately not trigger-specific: any row type that can legitimately
 * repeat adjacently with identical identity (a retried LLM call, a repeated
 * log line, …) adopts this with one `keyOf` + one `merge` function — no new
 * grouping logic. Stays a display-layer concern; callers decide where in
 * their render path to apply it, same as `coalesceAdjacentTriggers` does for
 * `ConversationTimeline`.
 */

/**
 * Partition `items` into runs of consecutive items sharing the same
 * `keyOf` result. `keyOf` returning `null` is a hard "never merge with a
 * neighbor" signal — used for items that should always stand alone even if a
 * neighbor happens to produce the same key by coincidence. Every run is
 * non-empty and order is preserved; a singleton item is a run of length 1.
 */
function groupAdjacent<T>(items: T[], keyOf: (item: T) => string | null): T[][] {
  const groups: { key: string | null; items: T[] }[] = [];
  for (const item of items) {
    const key = keyOf(item);
    const last = groups[groups.length - 1];
    if (key !== null && last && last.key === key) {
      last.items.push(item);
    } else {
      groups.push({ key, items: [item] });
    }
  }
  return groups.map((g) => g.items);
}

/**
 * Group adjacent same-identity items via {@link groupAdjacent}, then flatten
 * back to a same-shaped list: a run of length 1 passes its item through
 * UNCHANGED (same reference — cheap to test, cheap for React to bail out
 * of re-rendering), a run of length > 1 is replaced by one call to `merge`.
 * `merge` receives the whole run in original order (first…last) so it can
 * pick first-vs-latest per field itself (e.g. keep the first id for a stable
 * React key, but the latest timestamp/summary).
 */
export function coalesceAdjacent<T>(
  items: T[],
  keyOf: (item: T) => string | null,
  merge: (group: T[]) => T,
): T[] {
  return groupAdjacent(items, keyOf).map((group) => (group.length > 1 ? merge(group) : group[0]));
}
