/**
 * Indexing / progress screen — honest stateful progress.
 *
 * The old version showed a fake percentage that jumped to 96% the moment
 * scanning ended, then stalled with "Generating wiki pages…" for the
 * (much longer) page-writing phase. Customers complained, rightly.
 *
 * This screen now:
 *   - Drives the progress bar from real phase transitions emitted by the
 *     server (clone → scan → graph → plan → pages → finalize).
 *   - Shows sub-progress inside the two long phases that have it
 *     (scan: files/total; pages: pages/total-planned).
 *   - Replaces the file-scan list with a milestone log timeline so the
 *     user can see exactly what the indexer is doing right now.
 */

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useLocation } from "wouter";
import {
  AlertTriangle,
  CircleAlert,
  Dot,
  Info,
  Loader2,
  RotateCcw,
  TriangleAlert,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { ModelBrandIcon } from "@/components/ModelBrandIcon";
import { cn } from "@/lib/utils";
import { formatModelName } from "@/utils/model";

import { BrandMark } from "./BrandMark";
import { WikiTopBar } from "./WikiTopBar";
import {
  useCancelIndexing,
  useIndexingJob,
  useIndexingStream,
  useResumeIndexing,
} from "./api/hooks";
import type { IndexingStatus } from "./api/types";
import type { PlatformId } from "./router";
import { buildHref } from "./router";
import { IndexingProgress, PHASE_ORDER } from "./progress";

interface IndexingScreenProps {
  jobId?: string;
  slug?: string;
  platform?: PlatformId;
}

export function IndexingScreen({ jobId, slug, platform }: IndexingScreenProps) {
  const [, navigate] = useLocation();

  useEffect(() => {
    if (!jobId) {
      navigate(buildHref({ kind: "configure" }));
    }
  }, [jobId, navigate]);

  // Bumped after a successful resume to re-open the SSE stream in place
  // (same jobId) without unmounting the screen.
  const [resubscribeKey, setResubscribeKey] = useState(0);
  const stream = useIndexingStream(jobId ?? null, resubscribeKey);
  const cancel = useCancelIndexing();
  const resume = useResumeIndexing();
  const snapshot = useIndexingJob(jobId ?? null);
  const effectivePlatform: PlatformId | undefined =
    platform ?? (snapshot.data?.platform as PlatformId | undefined);

  useEffect(() => {
    if (stream.job?.status === "complete") {
      const target = stream.job.landingPageId ?? "core";
      navigate(
        buildHref({
          kind: "page",
          pageId: target,
          slug,
          platform: effectivePlatform,
        }),
      );
    }
  }, [stream.job?.status, stream.job?.landingPageId, effectivePlatform, navigate, slug]);

  const job = stream.job;
  const displaySlug = job?.slug ?? slug ?? "bearlike/Assistant";
  const displayModel = job?.model ?? snapshot.data?.model;

  // Phase + sub-progress → real percentage. Two transports feed the same
  // atomic class: SSE stream (fresh, but lags the reducer on mount until
  // it replays from idx 0) and the snapshot poll (authoritative because
  // ``emit_phase`` writes the snapshot in the same tick as the event).
  // Picking the view with the higher percent means: snapshot wins while
  // the stream is still catching up, stream takes over once it has. ETA
  // always comes from the snapshot — the stream doesn't carry
  // ``phaseStartedAt``.
  const { pct, phase, label, statusLine, etaSeconds, fromSnap } = useMemo(() => {
    const fromStream = IndexingProgress.fromStream({
      job: job ?? null,
      phase: stream.phase,
      pagesSubmitted: stream.pagesSubmitted,
      totalPages: stream.totalPages,
    });
    const snap = snapshot.data;
    const fromSnapInner = snap ? IndexingProgress.fromJob(snap) : null;
    const base =
      fromSnapInner && fromSnapInner.pct > fromStream.pct
        ? fromSnapInner
        : fromStream;
    return {
      ...base,
      etaSeconds:
        fromSnapInner && fromSnapInner.phase === base.phase
          ? fromSnapInner.etaSeconds
          : base.etaSeconds,
      fromSnap: fromSnapInner,
    };
  }, [
    stream.phase,
    stream.pagesSubmitted,
    stream.totalPages,
    job,
    snapshot.data,
  ]);

  const etaLabel = IndexingProgress.formatEta(etaSeconds);
  // "Waiting for the indexer to start" should ONLY appear when neither
  // transport reports any sign of life — otherwise the snapshot already
  // tells us the indexer is past clone/scan and the log timeline is just
  // lagging an SSE replay.
  const indexerHasStarted =
    stream.logs.length > 0 ||
    Boolean(fromSnap && fromSnap.pct > 0) ||
    Boolean(snapshot.data && snapshot.data.status !== "queued");

  // Terminal-but-incomplete: the run stopped (failed / interrupted /
  // cancelled) without finishing. We show a recovery panel instead of a
  // stuck-at-X% bar. The SSE stream surfaces a terminal ``error`` (folded
  // into ``stream.error``) or a ``cancelled`` status; the snapshot poll is
  // authoritative for ``failed``/``interrupted``. ``complete`` is excluded —
  // that path auto-navigates away above.
  const FAILED: ReadonlySet<IndexingStatus> = useMemo(
    () => new Set<IndexingStatus>(["failed", "interrupted", "cancelled"]),
    [],
  );
  const terminalStatus: IndexingStatus | null =
    snapshot.data && FAILED.has(snapshot.data.status)
      ? snapshot.data.status
      : job && FAILED.has(job.status)
        ? job.status
        : null;
  const isIncomplete = Boolean(terminalStatus) || Boolean(stream.error);
  const incompleteError =
    stream.error?.message ?? snapshot.data?.error?.message ?? null;

  const resumeIndexing = () => {
    if (!jobId || resume.isPending) return;
    resume.mutate(jobId, {
      // Same job resumes in place — re-open the stream so the screen
      // tracks the resumed run live again.
      onSuccess: () => setResubscribeKey((k) => k + 1),
    });
  };

  // Pin the log timeline to the bottom on each new entry so the user
  // always sees the latest milestone without manual scroll. Earlier
  // entries remain accessible by scrolling up.
  const logScrollRef = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const el = logScrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [stream.logs.length]);

  return (
    <div className="flex flex-col flex-1 overflow-y-auto">
      <WikiTopBar repo={displaySlug} showBackToAll />
      <div className="flex-1 px-4 sm:px-6 py-10 sm:py-16 flex items-start justify-center">
        <div className="w-full max-w-[720px] rounded-2xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] shadow-[0_8px_28px_rgba(0,0,0,0.16)] p-5 sm:p-6">
          {/* Header — brand, slug, current phase line, model, percent */}
          <div className="flex items-center gap-3.5">
            <span className={isIncomplete ? "text-amber-500" : "text-[hsl(var(--primary))]"}>
              {isIncomplete ? (
                <TriangleAlert size={28} />
              ) : (
                <BrandMark size={28} spin />
              )}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-sm font-semibold">
                {isIncomplete ? "Indexing stopped" : label}
              </div>
              <div className="mt-0.5 text-xs text-[hsl(var(--muted-foreground))] truncate">
                <span className="font-mono">{displaySlug}</span>
                {statusLine && (
                  <>
                    <span className="opacity-50 mx-1.5">·</span>
                    {statusLine}
                  </>
                )}
                {!isIncomplete && etaLabel && (
                  <>
                    <span className="opacity-50 mx-1.5">·</span>
                    <span className="tabular-nums">{etaLabel}</span>
                  </>
                )}
                {isIncomplete && (
                  <>
                    <span className="opacity-50 mx-1.5">·</span>
                    <span>reached {IndexingProgress.label(phase)}</span>
                  </>
                )}
              </div>
              {displayModel && (
                <div className="mt-1 inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))]">
                  <ModelBrandIcon modelId={displayModel} size={12} />
                  <span>Authored by</span>
                  <span className="font-mono text-[hsl(var(--foreground))]">
                    {formatModelName(displayModel)}
                  </span>
                </div>
              )}
            </div>
            <div
              className={cn(
                "text-lg font-semibold font-mono tabular-nums",
                isIncomplete ? "text-amber-500" : "text-[hsl(var(--primary))]",
              )}
            >
              {pct}%
            </div>
          </div>

          {/* Progress bar — amber + frozen when the run stopped incomplete,
              so the fill reads as "what completed" rather than live progress. */}
          <div className="mt-3 h-1 rounded-full bg-[hsl(var(--muted))]/60 overflow-hidden">
            <div
              className={cn(
                "h-full transition-[width] duration-500",
                isIncomplete
                  ? "bg-amber-500/70"
                  : "bg-gradient-to-r from-[hsl(var(--primary))] to-[hsl(var(--primary))]/70",
              )}
              style={{ width: `${pct}%` }}
            />
          </div>

          {/* Phase strip — small dots showing which phase we're in */}
          <div className="mt-2 flex items-center gap-1 text-[10px] uppercase tracking-wide text-[hsl(var(--muted-foreground))]">
            {PHASE_ORDER.map((p, i) => {
              const reached = PHASE_ORDER.indexOf(phase) >= i;
              return (
                <span key={p} className="inline-flex items-center gap-0.5">
                  <Dot
                    className={cn(
                      "h-3 w-3 -mx-1",
                      reached ? "text-[hsl(var(--primary))]" : "text-[hsl(var(--muted-foreground))]/30",
                    )}
                  />
                  <span className={cn(reached ? "" : "opacity-40")}>{p}</span>
                </span>
              );
            })}
          </div>

          {/* Recovery panel — terminal-but-incomplete run. Surfaces the
              error and a Resume button instead of a stuck progress bar; the
              percent above already shows what completed. Resume re-drives the
              same job and re-opens the stream in place. */}
          {isIncomplete && (
            <div className="mt-4 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3.5">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-amber-600">
                    {terminalStatus === "cancelled"
                      ? "Indexing was cancelled"
                      : terminalStatus === "interrupted"
                        ? "Indexing was interrupted"
                        : "Indexing failed"}
                  </p>
                  <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
                    {incompleteError ? (
                      <span className="font-mono break-words text-amber-600/90">
                        {incompleteError}
                      </span>
                    ) : (
                      "Resume to continue from where it stopped — completed pages and the knowledge graph are reused."
                    )}
                  </p>
                </div>
                {jobId && (
                  <Button
                    type="button"
                    variant="neutral"
                    size="sm"
                    tone="info"
                    disabled={resume.isPending}
                    onClick={resumeIndexing}
                    leadingIcon={
                      resume.isPending ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <RotateCcw className="h-3 w-3" />
                      )
                    }
                    className="shrink-0"
                  >
                    Resume indexing
                  </Button>
                )}
              </div>
            </div>
          )}

          {/* Log timeline — real milestone lines from the BE.
              Renders all logs (not a rolling last-N) so refreshing the
              page never shows a "different" subset. Auto-scrolls to the
              latest entry; users can scroll up to read history. */}
          <div
            ref={logScrollRef}
            className="mt-5 space-y-1 min-h-[230px] max-h-[280px] overflow-y-auto pr-1"
          >
            {stream.logs.length === 0 && (
              <div className="text-xs text-[hsl(var(--muted-foreground))] py-6 text-center">
                {indexerHasStarted
                  ? "Catching up on the indexer log…"
                  : "Waiting for the indexer to start…"}
              </div>
            )}
            {stream.logs.map((line, i) => (
              <div
                key={`${i}-${line.text}`}
                className={cn(
                  "flex items-start gap-2 px-2 py-1.5 rounded-md text-xs",
                  line.level === "error"
                    ? "text-red-500 bg-red-500/10"
                    : line.level === "warn"
                    ? "text-amber-600 bg-amber-500/10"
                    : "text-[hsl(var(--muted-foreground))]",
                )}
              >
                {line.level === "error" ? (
                  <CircleAlert className="h-3 w-3 mt-0.5 shrink-0" />
                ) : line.level === "warn" ? (
                  <AlertTriangle className="h-3 w-3 mt-0.5 shrink-0" />
                ) : (
                  <Loader2 className="h-3 w-3 mt-0.5 shrink-0 text-[hsl(var(--primary))]/60" />
                )}
                <span className="font-mono leading-relaxed flex-1">{line.text}</span>
              </div>
            ))}
          </div>

          {/* Footer — info + cancel */}
          <div className="mt-4 pt-3 border-t border-[hsl(var(--border))] flex items-center gap-3 flex-wrap">
            <div className="inline-flex items-center gap-1.5 text-[11px] text-[hsl(var(--muted-foreground))] flex-1 min-w-[200px]">
              <Info className="h-3 w-3" />
              Indexing typically takes a few minutes to half an hour. The page
              you'll land on opens automatically when ready.
            </div>
            {jobId && !isIncomplete && job?.status !== "complete" && job?.status !== "cancelled" && (
              <button
                type="button"
                onClick={() =>
                  cancel.mutate(jobId, {
                    onSuccess: () => navigate(buildHref({ kind: "landing" })),
                  })
                }
                disabled={cancel.isPending}
                className="text-[11px] text-[hsl(var(--muted-foreground))] hover:text-red-500 transition-colors px-2 py-1 rounded hover:bg-red-500/10"
              >
                Cancel indexing
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// (Progress / ETA / phase math lives in ``./progress.ts`` so the
// landing card and this page render from the exact same calculation.)
