import { useEffect, useState } from "react";
import { AlertTriangle, Check, Loader2, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { useFirePipeline } from "../../../hooks/useApps";
import { FirePipelineError, type PipelineLiveness } from "../../../api/apps";
import { describePipelineLiveness } from "../pipelineSchedule";
import { FLASH_ERROR_MS, FLASH_SUCCESS_MS } from "./flash";
import { RailList, RailMeta, RailRow, RailSkeleton, railLabelIconCls, type RailTone } from "./rows";

export function PipelinesBody({
  appId,
  pipelines,
  loading,
}: {
  appId: string;
  pipelines: PipelineLiveness[] | undefined;
  loading: boolean;
}) {
  if (loading) {
    return <RailSkeleton />;
  }
  // Additive field — an older server simply omits it, and
  // absence is not "no pipelines". InstrumentRail gates this section on the same
  // condition (`showPipelines`), so this null is defensive: never claim there
  // are no pipelines when the server just didn't report them.
  if (!pipelines || pipelines.length === 0) return null;
  return (
    <RailList>
      {pipelines.map((p) => (
        <PipelineRow key={p.name} appId={appId} pipeline={p} />
      ))}
    </RailList>
  );
}

/** Transient result of a `firePipeline` call for one row — same "flash, don't
 *  toast" convention as `Health.tsx`'s `RearmFlash`. `started` is its own kind
 *  (not folded into `success`) because the copy differs: a 202 hasn't produced
 *  anything yet, it only started a maintainer run the existing `/system` poll
 *  will surface later. */
type PipelineFireFlash = { kind: "success" | "started" | "error"; message: string };

const FLASH_TONE: Record<PipelineFireFlash["kind"], RailTone> = {
  success: "success",
  started: "muted",
  error: "warning",
};

/** One pipeline row: liveness text (schedule/on-demand/armed, from
 *  `describePipelineLiveness`) that a fire's flash result temporarily
 *  replaces, plus the "Run now" affordance itself. */
function PipelineRow({ appId, pipeline }: { appId: string; pipeline: PipelineLiveness }) {
  const { text, warn } = describePipelineLiveness(pipeline);
  const fire = useFirePipeline();
  const [flash, setFlash] = useState<PipelineFireFlash | null>(null);

  useEffect(() => {
    if (!flash) return;
    const t = window.setTimeout(() => setFlash(null), flash.kind === "error" ? FLASH_ERROR_MS : FLASH_SUCCESS_MS);
    return () => window.clearTimeout(t);
  }, [flash]);

  const handleFire = () => {
    setFlash(null);
    fire.mutate(
      { appId, pipeline: pipeline.name },
      {
        onSuccess: (result) => {
          setFlash(
            result.mode === "code"
              ? { kind: "success", message: "Refreshed" }
              : { kind: "started", message: "Refresh started" },
          );
        },
        onError: (error) => {
          const retry =
            error instanceof FirePipelineError && error.retryAfterSeconds != null
              ? ` — retry in ${error.retryAfterSeconds}s`
              : "";
          setFlash({ kind: "error", message: `${error.message}${retry}` });
        },
      },
    );
  };

  return (
    <RailRow
      label={pipeline.name}
      labelTitle={pipeline.name}
      trailing={
        flash ? (
          <RailMeta
            tone={FLASH_TONE[flash.kind]}
            title={flash.message}
            icon={flash.kind === "success" ? <Check aria-hidden className={railLabelIconCls} /> : undefined}
          >
            {flash.message}
          </RailMeta>
        ) : (
          <RailMeta
            tone={warn ? "warning" : "muted"}
            title={text}
            icon={warn ? <AlertTriangle aria-hidden className={railLabelIconCls} /> : undefined}
          >
            {text}
          </RailMeta>
        )
      }
      actions={
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          aria-label={`Run ${pipeline.name} now`}
          title="Run now"
          disabled={fire.isPending}
          onClick={handleFire}
          className="text-[hsl(var(--muted-foreground))]"
        >
          {fire.isPending ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <RefreshCw className="h-3 w-3" />
          )}
        </Button>
      }
    />
  );
}
