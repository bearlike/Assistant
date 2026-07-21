// Shared date-bucketing for the NavRail recents (Tasks + Search consume this —
// do not write a second copy). Buckets a timestamp into Today / Previous 7 days
// / Older, following the Aura drawer convention: empty buckets are dropped by
// the grouping helper, an absent/unparseable date falls into Older.

export type DateBucket = "Today" | "Previous 7 days" | "Older";

/** Bucket order, oldest-last — the render order the rail uses. */
export const DATE_BUCKET_ORDER: readonly DateBucket[] = [
  "Today",
  "Previous 7 days",
  "Older",
];

const DAY_MS = 86_400_000;

/**
 * Which bucket a timestamp belongs to, relative to `now` (injected so tests can
 * pin a fixed clock instead of patching `Date`). An absent or unparseable value
 * sorts into Older rather than throwing.
 */
export function bucketForDate(
  iso: string | null | undefined,
  now: number = Date.now(),
): DateBucket {
  if (!iso) return "Older";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "Older";
  const startOfToday = new Date(now).setHours(0, 0, 0, 0);
  if (t >= startOfToday) return "Today";
  if (t >= startOfToday - 7 * DAY_MS) return "Previous 7 days";
  return "Older";
}

/**
 * Partition `items` into buckets in `DATE_BUCKET_ORDER`, dropping empty ones.
 * Preserves each item's relative order within its bucket, so a newest-first
 * input stays newest-first per group.
 */
export function groupByDateBucket<T>(
  items: readonly T[],
  getDate: (item: T) => string | null | undefined,
  now: number = Date.now(),
): Array<{ bucket: DateBucket; items: T[] }> {
  const grouped = new Map<DateBucket, T[]>();
  for (const item of items) {
    const bucket = bucketForDate(getDate(item), now);
    const existing = grouped.get(bucket);
    if (existing) existing.push(item);
    else grouped.set(bucket, [item]);
  }
  return DATE_BUCKET_ORDER.flatMap((bucket) => {
    const bucketItems = grouped.get(bucket);
    return bucketItems ? [{ bucket, items: bucketItems }] : [];
  });
}
