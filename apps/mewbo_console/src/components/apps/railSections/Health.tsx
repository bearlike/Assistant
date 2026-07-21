import { useEffect, useState } from "react";
import { Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { useRearmApp } from "../../../hooks/useApps";
import type { AppSystemHealth } from "../../../api/apps";
import { RelativeTime } from "../../../utils/relativeTime";
import { FLASH_ERROR_MS, FLASH_SUCCESS_MS } from "./flash";
import {
  RAIL_TONE_CLS,
  RailEmpty,
  RailNote,
  RailSkeleton,
  RailStat,
  RailStatList,
} from "./rows";

/** Transient result of a `rearmApp` call, rendered inline beside the action
 *  and cleared on a timer — same "flash, don't toast" pattern as the copy
 *  buttons (`CopyButton`, `WikiTopBar`) elsewhere in the console. */
type RearmFlash = { kind: "success" | "error"; message: string };

export function HealthBody({
  appId,
  system,
  loading,
}: {
  appId: string;
  system: AppSystemHealth | undefined;
  loading: boolean;
}) {
  const rearm = useRearmApp();
  // Owned here, not in RearmAction: a successful `seed:true` rearm empties
  // `unscheduled_pipelines`, and the mutation's `onSettled` invalidation can
  // refetch `/system` well inside the flash window — if the flash lived in
  // RearmAction (mounted only while `unscheduled` is non-empty), the gate
  // going false would unmount it mid-flash, destroying the "N armed, M
  // seeded" confirmation exactly when the action worked. Keeping the state
  // (and its clear timer) here lets the container gate on the flash too.
  const [rearmFlash, setRearmFlash] = useState<RearmFlash | null>(null);

  useEffect(() => {
    if (!rearmFlash) return;
    const t = window.setTimeout(
      () => setRearmFlash(null),
      rearmFlash.kind === "error" ? FLASH_ERROR_MS : FLASH_SUCCESS_MS,
    );
    return () => window.clearTimeout(t);
  }, [rearmFlash]);

  const freshness = system?.freshness;
  const unscheduled = system?.unscheduled_pipelines;
  if (loading) {
    return <RailSkeleton />;
  }
  if (!freshness) {
    return <RailEmpty>No pipeline runs yet.</RailEmpty>;
  }

  const handleRearm = () => {
    setRearmFlash(null);
    rearm.mutate(
      { appId, seed: true },
      {
        onSuccess: (result) => {
          const parts: string[] = [];
          if (result.armed.length > 0) parts.push(`${result.armed.length} armed`);
          if (result.seeded.length > 0) parts.push(`${result.seeded.length} seeded`);
          setRearmFlash({ kind: "success", message: parts.length > 0 ? parts.join(", ") : "Already up to date" });
        },
        onError: (error) => setRearmFlash({ kind: "error", message: error.message }),
      },
    );
  };

  const unscheduledNames = unscheduled && unscheduled.length > 0 ? unscheduled : null;
  // Additive field — absent on a server predating it, or empty once the
  // latest run wrote to everything the app declares.
  const emptyCollections =
    freshness.unwritten_collections && freshness.unwritten_collections.length > 0
      ? freshness.unwritten_collections
      : null;
  const staleRefresh = freshness.stale || !freshness.last_success_at;

  return (
    <>
      <RailStatList>
        <RailStat
          label="Last refreshed"
          value={
            freshness.last_success_at
              ? RelativeTime.format(freshness.last_success_at)
              : "never"
          }
          tone={staleRefresh ? "warning" : "default"}
          title={freshness.last_success_at ? RelativeTime.tooltip(freshness.last_success_at) : undefined}
        />
        <RailStat
          label="Next refresh"
          value={freshness.next_fire_at ? RelativeTime.format(freshness.next_fire_at) : "not scheduled"}
          tone={freshness.next_fire_at ? "default" : "warning"}
          title={freshness.next_fire_at ? RelativeTime.tooltip(freshness.next_fire_at) : undefined}
        />
        <RailStat label="Maintainer" value={system?.maintainer.status ?? "idle"} />
      </RailStatList>
      {emptyCollections && (
        // The poster-child gap this section closes: the run above can read
        // "Last refreshed just now" while a declared collection a served page
        // reads never gets a document. Same warning tone as the other rows,
        // its own line since it names collections, not a timestamp. It wraps
        // rather than truncating — the value IS the list of names.
        <RailNote tone="warning" wrap className="px-2 pt-1">
          Never written: {emptyCollections.join(", ")}
        </RailNote>
      )}
      {(unscheduledNames || rearmFlash) && (
        <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-1 px-2 pt-1.5">
          {unscheduledNames ? (
            <RailNote tone="warning" wrap className="min-w-0 flex-1">
              Not scheduled: {unscheduledNames.join(", ")}
            </RailNote>
          ) : (
            <span />
          )}
          <RearmAction loading={rearm.isPending} flash={rearmFlash} onRearm={handleRearm} />
        </div>
      )}
    </>
  );
}

/** "Re-arm schedules" — seeds triggers for pipelines the server flagged as
 *  unscheduled. Always seeds (per the wire contract §"rearm") since a
 *  pipeline that has never fired has no data either; a pure schedule repair
 *  with no backfill isn't a case this affordance needs to distinguish.
 *  Purely presentational — `HealthBody` owns the mutation + flash state (see
 *  the comment there) so the flash survives the container's own gate. */
function RearmAction({
  loading,
  flash,
  onRearm,
}: {
  loading: boolean;
  flash: RearmFlash | null;
  onRearm: () => void;
}) {
  return (
    <div className="flex flex-none items-center gap-1.5">
      {flash && (
        <span
          className={cn(
            "text-sm",
            flash.kind === "error" ? RAIL_TONE_CLS.warning : RAIL_TONE_CLS.muted,
          )}
        >
          {flash.message}
        </span>
      )}
      <Button
        variant="ghost"
        size="sm"
        onClick={onRearm}
        disabled={loading}
        leadingIcon={
          loading ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )
        }
        className="flex-none px-2"
      >
        Re-arm schedules
      </Button>
    </div>
  );
}
