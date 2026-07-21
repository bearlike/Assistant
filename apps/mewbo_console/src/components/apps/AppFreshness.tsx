import { AlertTriangle, CalendarOff, Clock, DatabaseZap, RefreshCw } from "lucide-react";

import { cn } from "@/lib/utils";
import { useAppSystem } from "../../hooks/useApps";
import { RelativeTime } from "../../utils/relativeTime";

/**
 * Freshness + next-fire line for an app, read from the SAME system endpoint the
 * detail health strip uses (spec §2.6, DRY). Owns its own lazy query rather
 * than taking data as a prop — freshness is per-card and never worth blocking a
 * gallery render on.
 *
 * Renders nothing ONLY while there is no system data yet (loading / error) —
 * once the endpoint answers, it always says something. A live app with zero
 * runs and zero armed triggers used to fall through `!last_success_at &&
 * !next_fire_at` straight to a blank render, which reads as "fine" — the exact
 * silent failure that shipped. "Never refreshed" and "No refresh
 * schedule" are explicit warning states now, each independent so they can
 * co-occur. This is a deliberate departure from the wiki `FreshnessBadge`
 * stance of "stay silent until there's drift to report": an app that has never
 * run has no baseline to diff against, but "never ran" is itself the thing a
 * user needs to see, not a reason to say nothing.
 *
 * "Empty collections" is the poster-child case this whole component exists
 * for: a run can succeed (`stale=false`, "Last refreshed just now") while a
 * DECLARED collection a served page actually reads never gets a document —
 * every signal above stays green. `freshness.unwritten_collections` is the
 * server's answer; absent on an older server, so this chip is additive and
 * simply doesn't render when the field is missing or empty.
 *
 * The per-card burst on gallery mount mirrors the wiki freshness pattern and is
 * bounded server-side; do not add a bespoke client cache or request queue. If
 * galleries ever grow enough to matter, gate `enabled` on visibility.
 *
 * Layout: the chips WRAP rather than clip. The search `WorkspaceCard` meta
 * shelf is single-line-by-contract because its overflow is decorative source
 * avatars; every chip here is a warning, and a warning that has been clipped
 * out of the card is exactly the silence this component was built to end. Up to
 * three can co-occur, which does not fit one 260px grid track.
 */
export function AppFreshness({
  appId,
  className,
}: {
  appId: string;
  className?: string;
}) {
  const { data } = useAppSystem(appId);
  const freshness = data?.freshness;
  if (!freshness) return null;

  const { last_success_at, next_fire_at, stale, unwritten_collections } = freshness;
  const unscheduled = data.unscheduled_pipelines;
  const emptyCollections = unwritten_collections && unwritten_collections.length > 0
    ? unwritten_collections
    : null;

  return (
    <div
      className={cn(
        "flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-2xs text-[hsl(var(--muted-foreground))]",
        className,
      )}
    >
      {last_success_at ? (
        <span
          className={cn(
            "inline-flex items-center gap-1 whitespace-nowrap",
            stale && "text-[hsl(var(--warning))]",
          )}
          title={
            stale
              ? `Data may be stale — last successful refresh ${RelativeTime.tooltip(last_success_at)}`
              : `Last refreshed ${RelativeTime.tooltip(last_success_at)}`
          }
        >
          {stale ? (
            <AlertTriangle className="h-3 w-3 flex-none" />
          ) : (
            <RefreshCw className="h-3 w-3 flex-none" />
          )}
          {RelativeTime.format(last_success_at)}
        </span>
      ) : (
        <span
          className="inline-flex items-center gap-1 whitespace-nowrap text-[hsl(var(--warning))]"
          title="This app has never completed a successful data refresh"
        >
          <AlertTriangle className="h-3 w-3 flex-none" />
          Never refreshed
        </span>
      )}
      {next_fire_at ? (
        <span
          className="inline-flex items-center gap-1 whitespace-nowrap"
          title={`Next scheduled refresh ${RelativeTime.tooltip(next_fire_at)}`}
        >
          <Clock className="h-3 w-3 flex-none" />
          Next {RelativeTime.format(next_fire_at)}
        </span>
      ) : (
        <span
          className="inline-flex items-center gap-1 whitespace-nowrap text-[hsl(var(--warning))]"
          title={
            unscheduled && unscheduled.length > 0
              ? `No trigger is armed — ${unscheduled.join(", ")} won't refresh automatically`
              : "No trigger is armed to refresh this app automatically"
          }
        >
          <CalendarOff className="h-3 w-3 flex-none" />
          No refresh schedule
        </span>
      )}
      {emptyCollections && (
        <span
          className="inline-flex min-w-0 max-w-full items-center gap-1 text-[hsl(var(--warning))]"
          title={`Declared but never written: ${emptyCollections.join(", ")}`}
        >
          <DatabaseZap className="h-3 w-3 flex-none" />
          {/* A collection name is author-supplied and can be long — it
              truncates rather than pushing the chip past the card. */}
          <span className="truncate">
            {emptyCollections.length === 1
              ? `${emptyCollections[0]} empty`
              : `${emptyCollections.length} collections empty`}
          </span>
        </span>
      )}
    </div>
  );
}
