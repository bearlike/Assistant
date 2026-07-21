/**
 * Compact human line for one version's diff summary. Kept React-free (no
 * JSX) so it stays unit-testable in isolation, same convention as
 * `pipelineSchedule.ts` / `triggers/triggerFormat.ts`.
 */
import type { AppVersionSummary } from "../../types/apps";

function filesClause(summary: AppVersionSummary): string | null {
  const total = summary.files_added + summary.files_changed + summary.files_removed;
  if (total <= 0) return null;
  return total === 1 ? "1 file changed" : `${total} files changed`;
}

/** `prefix` is `"+"`/`"-"`/`""`; `suffix` (e.g. `"changed"`) trails the noun. */
function namedClause(
  prefix: string,
  names: string[],
  noun: string,
  suffix = "",
): string | null {
  if (names.length === 0) return null;
  const plural = names.length === 1 ? noun : `${noun}s`;
  const tail = suffix ? ` ${suffix}` : "";
  return `${prefix}${names.length} ${plural}${tail} (${names.join(", ")})`;
}

/**
 * One compact line summarizing a version's diff vs. its predecessor, e.g.
 * "2 files changed · +1 pipeline (meetings)". Returns null when every count
 * is zero and every list is empty — the caller renders nothing rather than
 * an empty line (never a fabricated "no changes" placeholder).
 */
export function describeVersionSummary(summary: AppVersionSummary): string | null {
  const parts = [
    filesClause(summary),
    namedClause("+", summary.pipelines_added, "pipeline"),
    namedClause("-", summary.pipelines_removed, "pipeline"),
    namedClause("", summary.pipelines_changed, "pipeline", "changed"),
    namedClause("+", summary.collections_added, "collection"),
    namedClause("-", summary.collections_removed, "collection"),
  ].filter((p): p is string => p !== null);
  return parts.length > 0 ? parts.join(" · ") : null;
}
