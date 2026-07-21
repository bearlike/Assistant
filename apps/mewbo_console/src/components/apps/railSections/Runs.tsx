import { CheckCircle2, Loader2, XCircle, type LucideIcon } from "lucide-react";

import { cn } from "@/lib/utils";
import type { PipelineRun } from "../../../types/apps";
import { RelativeTime } from "../../../utils/relativeTime";
import {
  RAIL_TONE_CLS,
  RailEmpty,
  RailList,
  RailMeta,
  RailRow,
  RailSkeleton,
  railRowIconCls,
} from "./rows";

/** Recent-runs rail body shows at most this many rows; the rest stay in the
 *  underlying `/system` payload, reachable only by re-querying. */
const MAX_RECENT_RUNS_SHOWN = 6;

export function RunsBody({ runs, loading }: { runs: PipelineRun[]; loading: boolean }) {
  if (loading) {
    return <RailSkeleton />;
  }
  if (runs.length === 0) {
    return <RailEmpty>No runs yet.</RailEmpty>;
  }
  return (
    <RailList>
      {runs.slice(0, MAX_RECENT_RUNS_SHOWN).map((run) => (
        <RunRow key={run.run_key} run={run} />
      ))}
    </RailList>
  );
}

/** Outcome → glyph + tone. Data, not logic: anything not terminal is still
 *  running, which is the same fallback the wire's three statuses have always
 *  had here. */
function outcomeGlyph(status: PipelineRun["status"]): {
  icon: LucideIcon;
  tone: string;
  spin: boolean;
} {
  if (status === "succeeded") return { icon: CheckCircle2, tone: RAIL_TONE_CLS.success, spin: false };
  if (status === "failed") return { icon: XCircle, tone: RAIL_TONE_CLS.danger, spin: false };
  return { icon: Loader2, tone: RAIL_TONE_CLS.active, spin: true };
}

/** The run's outcome as a leading state glyph. The GLYPH carries the state and
 *  the tone only reinforces it, so an outcome reads without colour; the status
 *  word rides along for assistive tech, since the icon alone names nothing. */
function RunOutcome({ status }: { status: PipelineRun["status"] }) {
  const { icon: Icon, tone, spin } = outcomeGlyph(status);
  return (
    <>
      <Icon aria-hidden className={cn(railRowIconCls, tone, spin && "animate-spin")} />
      <span className="sr-only">{status}</span>
    </>
  );
}

function RunRow({ run }: { run: PipelineRun }) {
  const docs = Object.values(run.docs_written ?? {}).reduce((a, b) => a + b, 0);
  const when = run.ended_at ?? run.started_at;
  return (
    <RailRow
      label={run.pipeline_name}
      labelTitle={run.pipeline_name}
      leading={<RunOutcome status={run.status} />}
      trailing={
        <>
          {run.status === "succeeded" && docs > 0 && (
            <RailMeta title={`${docs} documents written`}>+{docs}</RailMeta>
          )}
          <RailMeta title={RelativeTime.tooltip(when)}>{RelativeTime.format(when)}</RailMeta>
        </>
      }
    />
  );
}
