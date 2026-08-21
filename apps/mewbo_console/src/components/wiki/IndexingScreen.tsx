/**
 * Indexing / progress screen — one bounded panel, four fixed regions.
 *
 * ## Why it is shaped this way
 *
 * The screen answers, in this order: is it alive, what is it doing, how far in,
 * and what has it actually said. Everything below follows from keeping those
 * four answers in fixed places instead of stacking them down a growing page.
 *
 * The previous version appended each region beneath the last — header, phase
 * dots, the whole declared plan, then a log box — so the panel grew taller with
 * every phase the backend declared and the page itself scrolled (measured at
 * 1,225 px of content in an 882 px viewport, with the log box's own scrollbar
 * nested inside it). Two scrollbars, controls pushed below the fold, and the
 * plan and the log reading as unrelated lists that happened to be adjacent.
 *
 * So: the root never scrolls. The panel fills the viewport and splits into a
 * fixed header, a two-pane body where each pane owns its own bounded scroll
 * container, and a fixed footer. The plan and the activity log sit side by side
 * because they are two views of the same run — the plan is what the indexer
 * intends, the log is what it is saying — and the log's lines are grouped by
 * the declared step that produced them, which is what makes the pairing legible
 * rather than decorative.
 *
 * ## Cost
 *
 * `O(one record)` per render: one pass over one job's declared steps, plus a
 * bounded tail of its log. Neither grows with the run's history — the earlier
 * shape put 5,468 log rows and 22,205 DOM nodes on screen mid-index.
 */

import { useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { useLocation } from "wouter";
import { Info, Loader2, RotateCcw, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import { ModelBrandIcon } from "@/components/ModelBrandIcon";
import { cardSurface } from "@/components/ui/card-surface";
import { cn } from "@/lib/utils";
import { formatModelName } from "@/utils/model";

import { BrandMark } from "../BrandMark";
import { RefreshScopeSummary } from "./RefreshScopeSummary";
import { SessionJumpButton } from "./SessionJumpButton";
import { WikiTopBar } from "./WikiTopBar";
import { useCancelIndexing, useIndexingJob, useResumeIndexing } from "./api/hooks";
import { useIndexingStream } from "./api/streamHooks";
import type { IndexingStatus } from "./api/types";
import type { PlatformId } from "./router";
import { buildHref } from "./router";
import { IndexingProgress } from "./progress";
import { ActivityLog } from "./indexing/ActivityLog";
import { PhaseBar } from "./indexing/PhaseBar";
import { PlanOutline } from "./indexing/PlanOutline";
import { IndexingPlan } from "./indexing/planModel";
import { DEFAULT_WIKI_SLUG } from "./slug";

// --- Session jump -----------------------------------------------------------
// The affordance itself is the shared `SessionJumpButton` (the Q&A screen
// mounts the same one); only the wording is this surface's. It renders ONLY
// when the snapshot carries a `sessionId`, never disabled-with-tooltip: a
// graph-only index is deliberately sessionless, and there is a brief window at
// queue time before the indexer session is attached. It stays mounted for a
// stopped run too — a failed index is precisely when the transcript is worth
// reading.
const INDEXING_SESSION_JUMP = {
  label: "Watch the indexing session",
  title: "Open the Mewbo session running this index",
} as const;

/** How often the elapsed readouts re-derive while work is open. */
const TICK_MS = 1_000;

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
  const displaySlug = job?.slug ?? slug ?? DEFAULT_WIKI_SLUG;
  const displayModel = job?.model ?? snapshot.data?.model;
  // Snapshot-only: the job→session binding rides the job wire shape, not the
  // SSE event stream (see the BE `_job_wire` seam). The snapshot polls twice a
  // second while the job is live, so the jump appears as soon as the indexer
  // session is attached.
  const sessionId = snapshot.data?.sessionId;
  // Snapshot-only, same reasoning as `sessionId` above: stamped once at job
  // creation and never rewritten, so there is nothing for the SSE stream to
  // carry that the snapshot poll doesn't already have from the first fetch.
  const refreshDecision = snapshot.data?.refreshDecision;
  // `scopePreview` rides BOTH transports (one write, two reads — see the
  // type's own doc) — prefer whichever has it: the stream event arrives the
  // instant it's emitted, the snapshot poll lands within 500ms either way.
  const scopePreview = stream.job?.scopePreview ?? snapshot.data?.scopePreview;

  // Phase + sub-progress → real percentage. Two transports feed the same
  // atomic class: SSE stream (fresh, but lags the reducer on mount until
  // it replays from idx 0) and the snapshot poll (authoritative because
  // ``emit_phase`` writes the snapshot in the same tick as the event).
  // Picking the view with the higher percent means: snapshot wins while
  // the stream is still catching up, stream takes over once it has. ETA
  // always comes from the snapshot — the stream doesn't carry
  // ``phaseStartedAt``.
  const { pct, phase, label, statusLine, etaSeconds, elapsedSeconds, fromSnap, progressLedger } =
    useMemo(() => {
      const fromStream = IndexingProgress.fromStream({
        job: job ?? null,
        phase: stream.phase,
        pagesSubmitted: stream.pagesSubmitted,
        totalPages: stream.totalPages,
      });
      const snap = snapshot.data;
      const fromSnapInner = snap ? IndexingProgress.fromJob(snap) : null;
      const snapWins = Boolean(fromSnapInner && fromSnapInner.pct > fromStream.pct);
      const base = snapWins && fromSnapInner ? fromSnapInner : fromStream;
      return {
        ...base,
        etaSeconds:
          fromSnapInner && fromSnapInner.phase === base.phase
            ? fromSnapInner.etaSeconds
            : base.etaSeconds,
        fromSnap: fromSnapInner,
        // The ledger MUST come from whichever transport won the percentage,
        // not from a fixed preference. The `progress` event fires on a write
        // cadence while the snapshot polls twice a second, so a fixed
        // "stream first" rule renders a stale outline beside a fresh percent
        // — seen live as a header still naming an embedding step while the
        // activity log was already minting entities. One source per frame.
        progressLedger: snapWins
          ? (snap?.progress ?? job?.progress)
          : (job?.progress ?? snap?.progress),
      };
    }, [stream.phase, stream.pagesSubmitted, stream.totalPages, job, snapshot.data]);

  // Terminal-but-incomplete: the run stopped (failed / interrupted /
  // cancelled) without finishing. We show a recovery panel instead of a
  // stuck-at-X% bar. The SSE stream surfaces a terminal ``error`` (folded
  // into ``stream.error``) or a ``cancelled`` status; the snapshot poll is
  // authoritative for ``failed``/``interrupted``. ``complete`` is excluded —
  // that path auto-navigates away above. This status LITERAL is presentation
  // only (which panel to draw) — it is deliberately NOT consulted for
  // cancelability below: `interrupted` is stopped-but-incomplete AND still
  // server-active (see `IndexingJob.isActive`) at the same time, so a status
  // set can't answer both questions — that conflation is exactly what left a
  // stuck job with no reachable Cancel.
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

  // Elapsed readouts are derived, not stored, so they need a clock. It ticks
  // only while work is open — a stopped run's durations are already final, and
  // a screen left on a failed job should not re-render once a second forever.
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (isIncomplete) return;
    const timer = window.setInterval(() => setNow(Date.now()), TICK_MS);
    return () => window.clearInterval(timer);
  }, [isIncomplete]);

  const plan = useMemo(
    () => IndexingPlan.from(progressLedger, phase, now),
    [progressLedger, phase, now],
  );
  const activeStep = plan.activeStep;
  const stepLabels = useMemo(() => plan.labelsByKey, [plan]);

  // The header's headline is the open step when the ledger names one, because
  // "Embedding graph nodes" is a better answer to "what is it doing" than the
  // phase it belongs to. The counter beside it comes from the same step, so the
  // two can never describe different work.
  const headline = isIncomplete ? "Indexing stopped" : (activeStep?.label ?? label);
  const detail = activeStep ? IndexingPlan.stepDetail(activeStep, now) : statusLine;
  const etaLabel = IndexingProgress.formatEta(etaSeconds);
  const elapsedLabel =
    elapsedSeconds == null ? "" : `${IndexingProgress.formatElapsed(elapsedSeconds)} elapsed`;

  // "Waiting for the indexer to start" should ONLY appear when neither
  // transport reports any sign of life — otherwise the snapshot already
  // tells us the indexer is past clone/scan and the log timeline is just
  // lagging an SSE replay.
  const indexerHasStarted =
    stream.logs.length > 0 ||
    Boolean(fromSnap && fromSnap.pct > 0) ||
    Boolean(snapshot.data && snapshot.data.status !== "queued");

  // Cancelability is the server's call, not a local re-derivation from
  // `status`: `isActive` (`IndexingJob.isActive`) is the ONE authority. Read
  // ONLY off the snapshot poll — the SSE-folded `job` never carries it (each
  // stream event sets a handful of narrow fields, never the full wire
  // object) — and require an explicit `true` rather than `!== false`, so a
  // pre-rollout snapshot or a job that hasn't loaded yet fails closed instead
  // of flashing a Cancel button the server hasn't actually endorsed.
  const canCancel = Boolean(jobId) && snapshot.data?.isActive === true;

  const resumeIndexing = () => {
    if (!jobId || resume.isPending) return;
    resume.mutate(jobId, {
      // Same job resumes in place — re-open the stream so the screen
      // tracks the resumed run live again.
      onSuccess: () => setResubscribeKey((k) => k + 1),
    });
  };

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <WikiTopBar repo={displaySlug} showBackToAll />
      <div className="flex min-h-0 flex-1 justify-center px-3 py-3 sm:px-6 sm:py-5">
        <section
          className={cn(
            cardSurface({ radius: "modal", elevation: "elev-3" }),
            "flex h-full min-h-0 w-full max-w-[1040px] flex-col overflow-hidden",
          )}
          aria-label="Indexing progress"
        >
          {/* ── Header: the four answers, in fixed positions ─────────────── */}
          <header className="shrink-0 border-b border-[hsl(var(--border))] px-4 pb-3 pt-4 sm:px-6">
            <div className="flex items-start gap-3.5">
              <span
                className={cn(
                  "mt-0.5",
                  isIncomplete
                    ? "text-[hsl(var(--warning))]"
                    : "text-[hsl(var(--primary))]",
                )}
              >
                {isIncomplete ? <TriangleAlert size={26} /> : <BrandMark size={26} spin />}
              </span>
              <div className="min-w-0 flex-1">
                <h1 className="truncate text-base font-medium text-[hsl(var(--foreground))]">
                  {headline}
                </h1>
                <p className="mt-0.5 truncate text-xs text-[hsl(var(--muted-foreground))]">
                  <span className="font-mono">{displaySlug}</span>
                  {isIncomplete ? (
                    <MetaSegment>reached {IndexingProgress.label(phase)}</MetaSegment>
                  ) : (
                    <>
                      {detail && <MetaSegment>{detail}</MetaSegment>}
                      {elapsedLabel && (
                        <MetaSegment>
                          <span className="tabular-nums">{elapsedLabel}</span>
                        </MetaSegment>
                      )}
                      {etaLabel && (
                        <MetaSegment>
                          <span className="tabular-nums">{etaLabel}</span>
                        </MetaSegment>
                      )}
                    </>
                  )}
                </p>
                {displayModel && (
                  <p className="mt-1 inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
                    <ModelBrandIcon modelId={displayModel} size={12} />
                    <span>Authored by</span>
                    <span className="text-[hsl(var(--foreground))]">
                      {formatModelName(displayModel)}
                    </span>
                  </p>
                )}
              </div>
              <div
                className={cn(
                  "shrink-0 text-xl font-medium tabular-nums",
                  isIncomplete
                    ? "text-[hsl(var(--warning))]"
                    : "text-[hsl(var(--primary-text))]",
                )}
                aria-label={`${pct} percent complete`}
              >
                {pct}%
              </div>
            </div>

            {/* One positional readout, not two: each segment is a stage, and
                the stage doing work fills as its steps settle. */}
            <PhaseBar phases={plan.phases} stopped={isIncomplete} className="mt-3.5" />
          </header>

          {/* ── Recovery: the only thing that matters when a run stops ────── */}
          {isIncomplete && (
            <div className="shrink-0 border-b border-[hsl(var(--border))] bg-[hsl(var(--warning))]/5 px-4 py-3 sm:px-6">
              <div className="flex items-start gap-3">
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[hsl(var(--warning-text))]">
                    {terminalStatus === "cancelled"
                      ? "Indexing was cancelled"
                      : terminalStatus === "interrupted"
                        ? "Indexing was interrupted"
                        : "Indexing failed"}
                  </p>
                  {/* Bounded: a provider error can be arbitrarily long, and an
                      unbounded dump here is what would re-grow the panel. */}
                  <div className="mt-1 max-h-[104px] overflow-y-auto text-xs text-[hsl(var(--muted-foreground))]">
                    {incompleteError ? (
                      <span className="break-words font-mono text-[hsl(var(--warning-text))]">
                        {incompleteError}
                      </span>
                    ) : (
                      "Resume to continue from where it stopped — completed pages and the knowledge graph are reused."
                    )}
                  </div>
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

          {/* Refresh scope transparency — why a full rebuild was chosen, or
              what a scoped refresh actually touched. Self-nulls on a first
              index, so the band is gated on the decision existing at all
              rather than rendering as an empty strip. */}
          {refreshDecision && (
            <div className="shrink-0 border-b border-[hsl(var(--border))] px-4 py-2.5 sm:px-6">
              <RefreshScopeSummary
                refreshDecision={refreshDecision}
                scopePreview={scopePreview}
              />
            </div>
          )}

          {/* ── Body: two panes, each with its OWN scroll container. The page
                 has none — that is the whole point of the rebuild. ───────── */}
          <div
            className={cn(
              "grid min-h-0 flex-1",
              "grid-rows-[minmax(0,2fr)_minmax(0,3fr)] divide-y divide-[hsl(var(--border))]",
              "lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)] lg:grid-rows-1 lg:divide-x lg:divide-y-0",
            )}
          >
            <PlanOutline plan={plan} now={now} className="min-w-0" />
            <ActivityLog
              entries={stream.logs}
              labels={stepLabels}
              live={!isIncomplete}
              emptyMessage={
                indexerHasStarted
                  ? "Catching up on the indexer log…"
                  : "Waiting for the indexer to start…"
              }
              className="min-w-0"
            />
          </div>

          {/* ── Footer: what you can do, always reachable ─────────────────── */}
          <footer className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-t border-[hsl(var(--border))] px-4 py-2.5 sm:px-6">
            <p className="inline-flex min-w-[200px] flex-1 items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
              <Info className="h-3 w-3 shrink-0" aria-hidden />
              Indexing takes a few minutes to half an hour. You can leave this
              page — the wiki opens on its own when it is ready.
            </p>
            {sessionId ? (
              <SessionJumpButton sessionId={sessionId} {...INDEXING_SESSION_JUMP} />
            ) : (
              refreshDecision?.path === "scoped" && (
                /* TEXT, never a disabled control — a dead button says "this
                   should work and doesn't", which is exactly the misreading
                   here. The absence is CORRECT (a scoped refresh is sessionless
                   by design); it was simply unreadable as anything but a
                   flaky button, because 18 consecutive runs had shown one. */
                <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                  A scoped refresh runs without an agent session, so there is no
                  transcript to watch.
                </span>
              )
            )}
            {canCancel && (
              <button
                type="button"
                onClick={() =>
                  cancel.mutate(jobId as string, {
                    onSuccess: () => navigate(buildHref({ kind: "landing" })),
                  })
                }
                disabled={cancel.isPending}
                className="rounded px-2 py-1 text-2xs text-[hsl(var(--muted-foreground))] transition-colors hover:bg-[hsl(var(--destructive))]/10 hover:text-[hsl(var(--destructive-text))]"
              >
                Cancel indexing
              </button>
            )}
          </footer>
        </section>
      </div>
    </div>
  );
}

/** One `·`-separated segment of the header's meta line. */
function MetaSegment({ children }: { children: ReactNode }) {
  return (
    <>
      <span className="mx-1.5 opacity-50">·</span>
      {children}
    </>
  );
}

// (Progress / ETA / phase math lives in ``./progress.ts`` so the
// landing card and this page render from the exact same calculation;
// the plan's grouping and the log's windowing live in ``./indexing/``.)
