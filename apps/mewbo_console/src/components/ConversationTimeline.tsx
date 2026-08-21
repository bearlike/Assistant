import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ChevronDown, FoldVertical, Info, Link2, MoreHorizontal, Pencil, Shield, ShieldAlert } from 'lucide-react';
import { MessageBubble, MarkdownContent } from './MessageBubble';
import { AttachmentCards } from './AttachmentCards';
import { WidgetCard } from './WidgetCard';
import { GenerativeUICard } from './GenerativeUICard';
import { CopyButton } from './CopyButton';
import { SpeakButton } from './SpeakButton';
import { ScrollToBottom } from './ScrollToBottom';
import { TurnScroller } from './TurnScroller';
import { CompactionMeta, DiffFile, EventRecord, QuestionAnswerItemPayload, RecoveryMeta, SafetyPlaneMeta, SessionUsage, TimelineEntry, TurnMeta } from '../types';
import type { AnswerQuestionResult } from '../api/contracts';
import { FileList } from './FileList';
import { PlanCard } from './PlanCard';
import { ProjectSwitchCard } from './ProjectSwitchCard';
import { QuestionCard } from './QuestionCard';
import { RunFailedCard } from './RunFailedCard';
import { TodoCard } from './TodoCard';
import { SessionTerminatedDivider, TriggerTranscriptRow } from './triggers/TriggerTranscriptRow';
import { coalesceAdjacentTriggers } from './triggers/coalesceTriggers';
import { SummaryBlock } from './SummaryBlock';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { useElapsed } from '../hooks/useElapsed';
import { ModelLabel } from './ModelLabel';
import { Button } from './ui/button';
import { ContextWindowBar } from './ContextWindowBar';
import { formatTokens } from '../utils/time';
import { readBool, readNumber } from '../utils/payload';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

// Turn-completion clock for the footer's left info cluster: compact and
// today-relative. "2:32 PM" for a response generated today, "Jul 19, 2:32 PM"
// earlier this year, "Jul 19, 2025, 2:32 PM" in a prior year. Read-only "when
// the response was generated"; the exact instant rides the `<time>` title/
// dateTime. Local timezone via Intl — no dependency, no util-file churn.
const TS_TIME = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const TS_DATE_TIME = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
});
const TS_DATE_TIME_YEAR = new Intl.DateTimeFormat(undefined, {
  month: 'short',
  day: 'numeric',
  year: 'numeric',
  hour: 'numeric',
  minute: '2-digit',
});
function formatTurnTimestamp(iso: string, now = new Date()): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  if (
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate()
  ) {
    return TS_TIME.format(d);
  }
  if (d.getFullYear() === now.getFullYear()) return TS_DATE_TIME.format(d);
  return TS_DATE_TIME_YEAR.format(d);
}

/**
 * Per-row top padding by entry role — the turn-boundary rhythm. `assistant`
 * and `user` get their own conditional cases below (tighter when an assistant
 * row directly answers the user row above it; a bigger gap opens a new turn),
 * so they're excluded from this lookup.
 */
const ROLE_SPACING: Partial<Record<TimelineEntry['role'], string>> = {
  plan: 'pt-3',
  widget: 'pt-3',
  generative_ui: 'pt-3',
  todos: 'pt-3',
  question: 'pt-3',
  trigger: 'pt-2.5',
  recovery: 'pt-2.5',
  compaction: 'pt-2.5',
  project_switch: 'pt-3',
  run_failed: 'pt-3',
  session_terminated: 'pt-6',
};

interface ConversationTimelineProps {
  timeline: TimelineEntry[];
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta, file?: DiffFile) => void;
  activeTurnId?: string | null;
  isRunning?: boolean;
  /** The run has been accepted but has not opened its turn yet — renders the
   *  starting beat at the tail. Mutually exclusive with the pending beat by
   *  construction (that one needs an open turn). */
  isStarting?: boolean;
  /** Live assistant text streamed from the in-flight turn. */
  streamingText?: string;
  onShowActiveTrace?: () => void;
  /**
   * Live per-step chip on the active row, sourced from the SAME
   * `useThroughput` estimator `RunTelemetry` renders (`SessionDetailView`
   * threads it down here too) — no second estimator. `activePhase`/
   * `activeTokPerSec` gate whether the `≈ N tok/s` reading is shown;
   * `activeStepStartTs` is the elapsed clock's start.
   */
  activePhase?: string;
  activeTokPerSec?: number;
  activeStepStartTs?: string;
  onApprovePlan?: (approved: boolean) => void;
  onAnswerQuestion?: (
    callId: string,
    callToken: string,
    answers: QuestionAnswerItemPayload[],
    notes?: string,
  ) => Promise<AnswerQuestionResult>;
  onRetryFrom?: (fromTs: string) => void;
  onForkFrom?: (fromTs: string) => void;
  onForkSession?: () => void;
  onEditAndRegenerate?: (fromTs: string, newText: string) => void;
  /** Session-level recovery, offered by the one failure card named by
   *  {@link ConversationTimelineProps.recoverableFailureId}. */
  onRecover?: (action: 'retry' | 'continue', model?: string) => void;
  /** Id of the single `run_failed` entry allowed to offer live recovery.
   *  Every other failure card renders read-only. */
  recoverableFailureId?: string;
  model?: string;
  /** The session's declared fallback ladder — read by the failure card to
   *  propose the next rung when recovery is offered. */
  fallbackModels?: string[];
  sessionUsage?: SessionUsage | null;
  systemBlock?: {
    summary?: {
      text: string[];
      testing: { command: string; passed: boolean }[];
    };
  };
}

/**
 * Smart-collapse wrapper for assistant message bodies.
 *
 * - The latest assistant turn is always fully expanded — that's what the
 *   reader most likely wants to see.
 * - Older turns are clipped to ~480px with a soft mask fade. The toggle
 *   sits BELOW the bubble (per the design), not inside it, so the fade
 *   reads as a deliberate "more available" affordance, not a clipped
 *   render. ResizeObserver detects when content actually overflows so
 *   the toggle only appears when needed.
 */
/**
 * Mid-run entry point to the trace/logs panel. Shared by BOTH in-flight
 * assistant rows: the "Working…" beat before the first token and
 * the live streaming bubble after it. It must live on both rows, not just the
 * pending one — otherwise the only mid-run door into the trace panel vanishes
 * the instant streaming begins.
 */
function TracePill({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      className="pending-trace"
      onClick={onClick}
      title="Open trace"
    >
      <Activity className="w-2.5 h-2.5" aria-hidden />
      <span>Trace</span>
    </button>
  );
}

/**
 * Live "how long has this step been running" chip for the active row, plus
 * an honest `≈ N tok/s` reading while text is actually streaming. `elapsed`
 * ticks off the shared `useElapsed` clock (same cadence as every other
 * live timer in the console) against `stepStartTs` — see
 * `useThroughput.Throughput.stepStartTs` for why that timestamp spans the
 * whole step (call + any tool dispatch) rather than resetting at the call's
 * `llm_call_end`. The rate is gated to `phase === 'Streaming'`: Reasoning has
 * no text yet to estimate from, and a tool call produces none either, so
 * showing a rate there would read as more precise than the estimator is.
 */
function ActiveStepChip({
  stepStartTs,
  phase,
  tokPerSec,
}: {
  stepStartTs?: string;
  phase?: string;
  tokPerSec?: number;
}) {
  const elapsed = useElapsed(stepStartTs, true);
  if (!elapsed) return null;
  const showRate = phase === 'Streaming' && (tokPerSec ?? 0) > 0;
  const label = showRate ? `${elapsed} · ≈ ${formatTokens(tokPerSec)} tok/s` : elapsed;
  return <span className="tabular-nums">{label}</span>;
}

/**
 * Inline beat shown immediately after a user row while the agent has
 * accepted the turn but hasn't streamed a single token yet. Replaces the
 * old bordered "Working… / Open trace" pill — visually one continuous
 * beat with the user bubble, not a separate card.
 */
function PendingAssistantRow({
  onShowTrace,
  stepStartTs,
  phase,
  tokPerSec,
}: {
  onShowTrace?: () => void;
  stepStartTs?: string;
  phase?: string;
  tokPerSec?: number;
}) {
  return (
    <div className="pt-1">
      <div className="pending-line" role="status" aria-live="polite">
        <span className="pending-dot" aria-hidden />
        <span className="pending-label">Working</span>
        <ActiveStepChip stepStartTs={stepStartTs} phase={phase} tokPerSec={tokPerSec} />
        {onShowTrace && <TracePill onClick={onShowTrace} />}
      </div>
    </div>
  );
}

/**
 * Inline beat for the window between a run being accepted and it opening its
 * turn — the orchestrator's setup, before any event the transcript can render.
 * Same vocabulary as {@link PendingAssistantRow} (it is the same class of
 * transient in-flight state, one beat earlier) minus the trace pill: there is
 * no turn to open a trace on yet.
 *
 * The copy states only what is known — the run was accepted and has not
 * produced anything yet. It never stands in for the prompt or the answer.
 */
function StartingRunRow() {
  return (
    <div className="pt-4">
      <div className="pending-line" role="status" aria-live="polite">
        <span className="pending-dot" aria-hidden />
        <span className="pending-label">Starting…</span>
      </div>
    </div>
  );
}

/**
 * Live assistant bubble that grows as `agent_message_delta` tokens stream in.
 * Renders the same markdown surface as a settled assistant
 * message plus a blinking caret (reusing the `wiki-caret` keyframe already in
 * index.css). Mounts only while the active turn is running and tokens have
 * arrived; the turn-closing `assistant` bubble built by `buildTimeline`
 * supersedes it, so the final text is authoritative and never duplicated.
 *
 * Carries the same `TracePill` as `PendingAssistantRow` so the trace panel
 * stays reachable for the whole in-flight turn, not just before streaming
 * starts.
 */
function StreamingAssistantRow({
  text,
  onShowTrace,
  stepStartTs,
  phase,
  tokPerSec,
}: {
  text: string;
  onShowTrace?: () => void;
  stepStartTs?: string;
  phase?: string;
  tokPerSec?: number;
}) {
  return (
    <div className="pt-1.5" role="status" aria-live="polite" aria-busy="true">
      <div className="text-[hsl(var(--foreground))] text-sm">
        <MarkdownContent content={text} />
        <span
          aria-hidden="true"
          className="inline-block w-[2px] h-[1em] align-text-bottom -mb-px ml-px bg-[hsl(var(--primary))] animate-[wiki-caret_900ms_steps(2)_infinite]"
        />
      </div>
      {(onShowTrace || stepStartTs) && (
        <div className="pending-line mt-1">
          <ActiveStepChip stepStartTs={stepStartTs} phase={phase} tokPerSec={tokPerSec} />
          {onShowTrace && <TracePill onClick={onShowTrace} />}
        </div>
      )}
    </div>
  );
}

const RECOVERY_LABELS: Record<RecoveryMeta['action'], string> = {
  retry: 'Retried from here',
  continue: 'Run continued',
};

/**
 * Compact marker for a session-level recovery the user triggered on a failed
 * run. Deliberately NOT a card — same quiet weight class as the trigger rows,
 * a lifecycle beat in the conversation flow. The link glyph reads as "the
 * thread resumes across this break", which is the one thing the surrounding
 * rows cannot say on their own: without it a failure card followed by another
 * user bubble looks like the user simply typed again.
 */
function RecoveryMarkerRow({ recovery }: { recovery: RecoveryMeta }) {
  return (
    <div
      className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]"
      role="note"
    >
      <span className="inline-flex items-center gap-1.5 font-medium text-[hsl(var(--primary-text))]">
        <Link2 className="w-3.5 h-3.5" aria-hidden />
        {RECOVERY_LABELS[recovery.action]}
      </span>
    </div>
  );
}

/**
 * Compact marker for a context-compaction boundary: the runtime replaced
 * older transcript events with a summary so the next model call stays
 * within its context budget. Same quiet weight class as
 * {@link RecoveryMarkerRow} — a lifecycle beat, not a card.
 *
 * Wording is deliberate: the transcript above this row is untouched and
 * still fully rendered — only what the MODEL receives going forward
 * narrows to that summary plus a recent window. "Summarized", never
 * "deleted"/"removed"/"cleared" — nothing was, and this line is the one
 * place in the conversation pane a reader could otherwise conclude it was.
 * Exported (unlike `RecoveryMarkerRow`) so it has a render test that
 * doesn't have to mount the whole timeline, with `TurnScroller`'s
 * IntersectionObserver dependency, just to reach one row.
 */
export function CompactionMarkerRow({ compaction }: { compaction: CompactionMeta }) {
  const freed = compaction.tokensSaved && compaction.tokensSaved > 0
    ? `${compaction.tokensSaved.toLocaleString()} tokens freed`
    : undefined;
  return (
    <div
      className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))]"
      role="note"
      title="Older messages are represented by a summary in the model's next request. The transcript above is unchanged."
    >
      <span className="inline-flex items-center gap-1.5 font-medium text-[hsl(var(--primary-text))]">
        <FoldVertical className="w-3.5 h-3.5" aria-hidden />
        Context compacted — earlier turns summarized, not removed
      </span>
      {freed && (
        <>
          <span aria-hidden className="opacity-40">
            ·
          </span>
          <span className="opacity-80">{freed}</span>
        </>
      )}
    </div>
  );
}

/**
 * Compact marker for the safety plane. Same quiet weight class as
 * {@link RecoveryMarkerRow} — a `role="note"` lifecycle beat, not a card,
 * because disclosure must be SEEN, not force expanded reading. `disclosed`
 * fires once, listing every active rule and what it inspects, BEFORE any of
 * them evaluate anything — the disclosure-before-invocation contract this row
 * exists to satisfy. `deny` names the rule and reason that stopped a call.
 */
function SafetyPlaneMarkerRow({ safetyPlane }: { safetyPlane: SafetyPlaneMeta }) {
  if (safetyPlane.phase === 'deny') {
    return (
      <div
        className="flex items-center gap-2 text-xs text-[hsl(var(--warning-text))]"
        role="note"
      >
        <span className="inline-flex items-center gap-1.5 font-medium">
          <ShieldAlert className="w-3.5 h-3.5" aria-hidden />
          Blocked by safety policy &ldquo;{safetyPlane.rule}&rdquo;
        </span>
        {safetyPlane.reason && <span className="opacity-80">{safetyPlane.reason}</span>}
      </div>
    );
  }
  return (
    <div
      className="flex flex-col gap-1 text-xs text-[hsl(var(--muted-foreground))]"
      role="note"
      title="Every tool call is checked against these rules before it runs."
    >
      <span className="inline-flex items-center gap-1.5 font-medium text-[hsl(var(--primary-text))]">
        <Shield className="w-3.5 h-3.5" aria-hidden />
        Safety plane active — {safetyPlane.rules.length} rule
        {safetyPlane.rules.length === 1 ? '' : 's'}
      </span>
      <ul className="ml-5 list-disc opacity-80">
        {safetyPlane.rules.map((rule) => (
          <li key={rule.name}>
            {rule.name}: {rule.inspects}
          </li>
        ))}
      </ul>
    </div>
  );
}

function SmartCollapse({
  children,
  isLatest,
}: {
  children: React.ReactNode;
  isLatest: boolean;
}) {
  const bodyRef = useRef<HTMLDivElement | null>(null);
  const [overflowing, setOverflowing] = useState(false);
  const [collapsed, setCollapsed] = useState(!isLatest);
  // Latest gets a generous limit; older clip aggressively. 140 px is small
  // enough that the mask-fade is actually visible (most older responses
  // exceed it), so the fade reads as a deliberate "more available" hint
  // instead of a clipping bug. (§N, P4)
  const clip = isLatest ? 9999 : 140;

  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const measure = () => setOverflowing(el.scrollHeight > clip);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [clip]);

  // Auto-expand the latest assistant body whenever the "is latest" status flips.
  useEffect(() => {
    if (isLatest) setCollapsed(false);
  }, [isLatest]);

  const isCollapsed = !isLatest && overflowing && collapsed;

  return (
    <div
      className={`session-collapsible ${isCollapsed ? 'is-collapsed' : ''}`}
      style={{ ['--clip' as string]: `${clip}px` }}
    >
      <div ref={bodyRef} className="session-collapsible-body">
        {children}
      </div>
      {!isLatest && overflowing && (
        <button
          type="button"
          onClick={() => setCollapsed((c) => !c)}
          className="mt-2 inline-flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/40 transition-colors"
          aria-expanded={!isCollapsed}
        >
          <ChevronDown
            className={`h-3.5 w-3.5 transition-transform duration-200 ${isCollapsed ? '' : 'rotate-180'}`}
          />
          <span>{isCollapsed ? 'Show full response' : 'Collapse'}</span>
        </button>
      )}
    </div>
  );
}

/**
 * Average output-generation throughput for a settled turn: Σ `output_tokens`
 * ÷ Σ `duration_ms` across the turn's root (depth 0) SUCCESSFUL
 * `llm_call_end` events, in tokens/sec. Deliberately footer-local rather than
 * folded into `computeTurnTokenUsage` (`utils/timeline.ts`) — that shape is
 * parity-gated against the Python transcript assembler via the corpus
 * fixture, and `duration_ms` is a display-only stat with no wire meaning
 * there. Only events carrying BOTH a positive `duration_ms` and an
 * `output_tokens` count are summed into either side of the ratio — an event
 * with no `duration_ms` (an older session, before the field shipped) has an
 * unknown time contribution, so folding its tokens into the numerator alone
 * would skew the average rather than just omit an unmeasured sample. Returns
 * undefined when no qualifying event exists (old sessions, a turn resolved
 * entirely by failed calls, or a call reporting `duration_ms: 0`).
 */
function computeTurnAvgTokPerSec(events: EventRecord[]): number | undefined {
  let totalOutput = 0;
  let totalMs = 0;
  for (const e of events) {
    if (e.type !== 'llm_call_end') continue;
    if ((readNumber(e.payload, 'depth') ?? 0) !== 0) continue;
    if (readBool(e.payload, 'success') !== true) continue;
    const durationMs = readNumber(e.payload, 'duration_ms') ?? 0;
    if (durationMs <= 0) continue;
    totalOutput += readNumber(e.payload, 'output_tokens') ?? 0;
    totalMs += durationMs;
  }
  return totalMs > 0 ? (totalOutput / totalMs) * 1000 : undefined;
}

/**
 * Per-turn footer for assistant messages — ONE strip, nothing else beneath the
 * message. Strict two-sided layout:
 *   LEFT  (read-only info): model · when generated · duration · tokens · avg
 *                            tok/s (successful calls only) · context
 *   RIGHT (interactive):    CopyButton · SpeakButton · "Trace" · "⋯" overflow
 * `SpeakButton` renders nothing when the server advertises no speech, so the
 * cluster is Copy/Trace/⋯ on a deployment without a speech gateway.
 * The whole strip (copy included) idles at `opacity-0` and reveals only on
 * `group-hover/turn` or `group-focus-within/turn` (the a11y law — keyboard
 * users get it via focus). No always-on `isLatest` case: the latest turn hides
 * its footer too. Copy lives ONLY here now; the bubble's own copy is suppressed
 * via `MessageBubble showCopy={false}` when this footer renders.
 *
 * Exported for tests only — same reason `CompactionMarkerRow` is: mounting the
 * whole `ConversationTimeline` pulls in `TurnScroller`'s `IntersectionObserver`,
 * which this suite's jsdom does not polyfill.
 */
export function AssistantTurnFooter({
  turn,
  model,
  sessionUsage,
  onShowTrace,
  onOpenFiles,
  onRetryFrom,
  onForkFrom,
  onForkSession,
  responseText,
  isRunning,
}: {
  turn: TurnMeta;
  model?: string;
  sessionUsage?: SessionUsage | null;
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta) => void;
  onRetryFrom?: (fromTs: string) => void;
  onForkFrom?: (fromTs: string) => void;
  onForkSession?: () => void;
  responseText: string;
  isRunning?: boolean;
}) {
  const fromTs = turn.events[0]?.ts;
  const usage = turn.tokenUsage;
  const tokensLine = useMemo(() => {
    if (!usage) return null;
    const peak = Math.max(usage.inputTokens, usage.subInputTokens);
    return peak > 0 ? `${formatTokens(peak)} in · ${formatTokens(usage.outputTokens)} out` : null;
  }, [usage]);
  // "When the response was generated" — the turn's closing completion event's
  // ts (`ts` IS the message id in this wire contract), falling back to the last
  // event of the turn. Read-only, so it belongs in the left info cluster.
  const generatedTs = useMemo(() => {
    const events = turn.events;
    if (!events || events.length === 0) return undefined;
    for (let i = events.length - 1; i >= 0; i--) {
      if (events[i].type === 'completion') return events[i].ts;
    }
    return events[events.length - 1]?.ts;
  }, [turn.events]);
  const avgTokPerSec = useMemo(() => computeTurnAvgTokPerSec(turn.events), [turn.events]);

  return (
    <div className="mt-4 pt-2 flex items-center justify-between gap-3 text-xs text-[hsl(var(--muted-foreground))] opacity-0 transition-opacity duration-150 group-hover/turn:opacity-100 group-focus-within/turn:opacity-100">
      {/* LEFT — read-only info: model · when generated · duration · tokens · context */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0">
        <ModelLabel modelId={turn.model ?? model} className="opacity-80" />
        {generatedTs && (
          <>
            <span className="opacity-40">·</span>
            <time dateTime={generatedTs} title={generatedTs} className="opacity-80">
              {formatTurnTimestamp(generatedTs)}
            </time>
          </>
        )}
        {turn.duration && (
          <>
            <span className="opacity-40">·</span>
            <span className="opacity-80">{turn.duration}</span>
          </>
        )}
        {tokensLine && (
          <>
            <span className="opacity-40">·</span>
            <span className="opacity-70" title="Peak input tokens · output tokens for this turn">
              {tokensLine}
            </span>
          </>
        )}
        {avgTokPerSec !== undefined && (
          <>
            <span className="opacity-40">·</span>
            <span
              className="opacity-70"
              title="Average output tokens/sec across this turn's successful model calls"
            >
              {avgTokPerSec.toFixed(1)} tok/s
            </span>
          </>
        )}
        {sessionUsage && sessionUsage.root_max_input_tokens > 0 && (
          <>
            <span className="opacity-40">·</span>
            <ContextWindowBar usage={sessionUsage} />
          </>
        )}
      </div>
      {/* RIGHT — interactive controls: copy · read aloud · Trace · overflow */}
      <div className="flex items-center gap-1 shrink-0">
        <CopyButton text={responseText} label="Copy response" className="h-7 w-7 rounded-md" />
        <SpeakButton text={responseText} label="Read response aloud" className="h-7 w-7 rounded-md" />
        <Button
          variant="ghost"
          size="sm"
          onClick={() => onShowTrace(turn)}
          className="h-7 rounded-md px-2.5 text-xs"
        >
          Trace
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              aria-label="More turn actions"
              title="More turn actions"
              className="h-7 w-7 rounded-md"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            {turn.files.length > 0 && (
              <DropdownMenuItem onSelect={() => onOpenFiles(turn)}>
                Open files ({turn.files.length})
              </DropdownMenuItem>
            )}
            {!isRunning && fromTs && onRetryFrom && (
              <DropdownMenuItem onSelect={() => onRetryFrom(fromTs)}>
                Retry from here
              </DropdownMenuItem>
            )}
            {!isRunning && fromTs && onForkFrom && (
              <DropdownMenuItem onSelect={() => onForkFrom(fromTs)}>
                Branch in new chat
              </DropdownMenuItem>
            )}
            {!isRunning && onForkSession && (
              <DropdownMenuItem onSelect={() => onForkSession()}>
                Fork session
              </DropdownMenuItem>
            )}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </div>
  );
}

export function ConversationTimeline({
  timeline,
  onShowTrace,
  onOpenFiles,
  activeTurnId,
  isRunning = false,
  isStarting = false,
  streamingText,
  onShowActiveTrace,
  activePhase,
  activeTokPerSec,
  activeStepStartTs,
  onApprovePlan,
  onAnswerQuestion,
  onRetryFrom,
  onForkFrom,
  onForkSession,
  onEditAndRegenerate,
  onRecover,
  recoverableFailureId,
  model,
  fallbackModels,
  sessionUsage,
  systemBlock,
}: ConversationTimelineProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  const startEdit = useCallback((id: string, content: string) => {
    setEditingId(id);
    setEditText(content);
  }, []);
  const submitEdit = useCallback(
    (fromTs: string) => {
      if (!editText.trim() || !onEditAndRegenerate) return;
      onEditAndRegenerate(fromTs, editText.trim());
      setEditingId(null);
      setEditText('');
    },
    [editText, onEditAndRegenerate],
  );
  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setEditText('');
  }, []);

  // Adjacent same-identity trigger rows (a repeating time.cron fire, etc.)
  // fold into one "×N times" row here — a display-only transform, so the
  // parsed `timeline` prop (and the Python-ported parser it mirrors) never
  // sees it. Every index/length below the map itself must read off THIS
  // array, not the raw prop, so `data-turn-idx` (TurnScroller's jump target)
  // and the row count TurnScroller renders stay in lockstep with what's
  // actually on screen.
  const displayTimeline = useMemo(() => coalesceAdjacentTriggers(timeline), [timeline]);

  const awaitingPlanApproval = useMemo(() => {
    if (displayTimeline.length === 0) return false;
    const last = displayTimeline[displayTimeline.length - 1];
    return last.role === 'plan' && last.plan?.status === 'pending';
  }, [displayTimeline]);

  // Index of the latest assistant entry — only that one defaults expanded.
  const latestAssistantIdx = useMemo(() => {
    for (let i = displayTimeline.length - 1; i >= 0; i--) {
      if (displayTimeline[i].role === 'assistant') return i;
    }
    return -1;
  }, [displayTimeline]);

  const { scrollRef, isAtBottom, scrollToBottom, onScroll } = useAutoScroll(displayTimeline.length);

  return (
    <div className="conv-scroll relative flex-1 overflow-hidden">
      <TurnScroller scrollRef={scrollRef} timeline={displayTimeline} />
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-y-auto"
      >
        <div className="mx-auto w-full max-w-[820px] px-6 pt-6 pb-32">
        {displayTimeline.map((entry, idx) => {
          // Per-row spacing rules:
          //   • Default: ~18px vertical breathing room.
          //   • Assistant follows its own user → tighter top (it's an answer).
          //   • Next user-after-first → turn boundary: bigger top + tiny margin.
          const prev = idx > 0 ? displayTimeline[idx - 1] : undefined;
          let spacing = 'pt-4';
          if (entry.role === 'assistant' && prev?.role === 'user') spacing = 'pt-1.5';
          else if (entry.role === 'user' && idx > 0) spacing = 'pt-8 mt-2';
          else spacing = ROLE_SPACING[entry.role] ?? spacing;

          const rowClass = `session-row-in group/turn flex flex-col ${spacing}`;
          const rowProps = {
            key: entry.id,
            'data-turn-idx': idx,
            style: { ['--row-index' as string]: Math.min(idx, 14) }, // cap stagger so very long sessions don't pause
            className: rowClass,
          };

          if (entry.role === 'widget') {
            return <div {...rowProps}>{entry.widget && <WidgetCard widget={entry.widget} />}</div>;
          }

          if (entry.role === 'generative_ui') {
            return (
              <div {...rowProps}>
                {entry.generativeUi && <GenerativeUICard ui={entry.generativeUi} />}
              </div>
            );
          }

          if (entry.role === 'plan') {
            return (
              <div {...rowProps}>
                {entry.plan && <PlanCard plan={entry.plan} onApprove={onApprovePlan} />}
              </div>
            );
          }

          if (entry.role === 'todos') {
            return (
              <div {...rowProps}>
                {entry.todos && <TodoCard todos={entry.todos} />}
              </div>
            );
          }

          if (entry.role === 'question') {
            return (
              <div {...rowProps}>
                {entry.question && (
                  <QuestionCard question={entry.question} onAnswer={onAnswerQuestion} />
                )}
              </div>
            );
          }

          if (entry.role === 'trigger') {
            return (
              <div {...rowProps}>
                {entry.trigger && <TriggerTranscriptRow trigger={entry.trigger} />}
              </div>
            );
          }

          if (entry.role === 'recovery') {
            return (
              <div {...rowProps}>
                {entry.recovery && <RecoveryMarkerRow recovery={entry.recovery} />}
              </div>
            );
          }

          if (entry.role === 'safety_plane') {
            return (
              <div {...rowProps}>
                {entry.safetyPlane && <SafetyPlaneMarkerRow safetyPlane={entry.safetyPlane} />}
              </div>
            );
          }

          if (entry.role === 'compaction') {
            return (
              <div {...rowProps}>
                {entry.compaction && <CompactionMarkerRow compaction={entry.compaction} />}
              </div>
            );
          }

          if (entry.role === 'project_switch') {
            return (
              <div {...rowProps}>
                {entry.projectSwitch && (
                  <ProjectSwitchCard meta={entry.projectSwitch} timestamp={entry.ts} />
                )}
              </div>
            );
          }

          if (entry.role === 'session_terminated') {
            return (
              <div {...rowProps}>
                <SessionTerminatedDivider ts={entry.ts} />
              </div>
            );
          }

          if (entry.role === 'run_failed') {
            // A failed turn keeps its footer: the trace link, per-turn actions
            // and token meta are exactly as relevant as on a turn that landed.
            const live = Boolean(onRecover) && entry.id === recoverableFailureId;
            return (
              <div {...rowProps}>
                {entry.runFailure && (
                  <RunFailedCard
                    failure={entry.runFailure}
                    timestamp={entry.ts}
                    model={model}
                    fallbackModels={fallbackModels}
                    onRetry={live ? (m) => onRecover?.('retry', m) : undefined}
                    onContinue={live ? (m) => onRecover?.('continue', m) : undefined}
                  />
                )}
                {entry.turn && (
                  <AssistantTurnFooter
                    turn={entry.turn}
                    model={model}
                    sessionUsage={sessionUsage}
                    onShowTrace={onShowTrace}
                    onOpenFiles={(t) => onOpenFiles(t)}
                    onRetryFrom={onRetryFrom}
                    onForkFrom={onForkFrom}
                    onForkSession={onForkSession}
                    responseText={entry.runFailure?.text ?? ''}
                    isRunning={isRunning}
                  />
                )}
              </div>
            );
          }

          if (entry.role === 'user') {
            const showPending =
              entry.turnId === activeTurnId && isRunning && !awaitingPlanApproval;
            return (
              <Fragment key={entry.id}>
              <div {...rowProps}>
                <div className="flex justify-end">
                  <div
                    className={`${editingId === entry.id ? 'flex w-full' : 'inline-flex'} flex-col items-end max-w-[min(640px,80%)]`}
                  >
                    {editingId === entry.id ? (
                      <div className="w-full space-y-2">
                        {/* Edit-state terminal card. The chevron + monospace
                            label + kbd hints set the "now you're editing" mood
                            without leaving the calm-reading surface vocabulary.
                            (§U, P3) */}
                        <div
                          className="w-full rounded-lg border bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))]"
                          style={{
                            borderColor: 'hsl(var(--primary) / 0.5)',
                            // Same shape as the composer family's own
                            // `:focus-within` rule (index.css) — elevation
                            // token first, then the primary focus ring. The
                            // old fixed `hsl(0 0% 0% / 0.32)` lift shadow
                            // ignored the light theme entirely.
                            boxShadow: 'var(--elev-3), 0 0 0 4px hsl(var(--primary) / 0.10)',
                          }}
                        >
                          <div className="flex items-center justify-between gap-2 px-4 pt-2.5 pb-1.5">
                            <span className="text-xs text-[hsl(var(--primary-text))] inline-flex items-center gap-2">
                              <span aria-hidden>›</span>
                              <span className="text-[hsl(var(--code-fg))]">Editing your message</span>
                            </span>
                            <span className="inline-flex items-center gap-1.5 text-[hsl(var(--muted-foreground))]">
                              <span className="edit-kbd">⌘ ↵</span>
                              <span className="text-2xs">save</span>
                              <span className="opacity-40">·</span>
                              <span className="edit-kbd">esc</span>
                              <span className="text-2xs">cancel</span>
                            </span>
                          </div>
                          <textarea
                            value={editText}
                            onChange={(e) => setEditText(e.target.value)}
                            className="w-full min-h-[80px] bg-transparent text-field md:text-sm leading-relaxed text-[hsl(var(--code-fg))] resize-y focus:outline-none px-4 py-2"
                            autoFocus
                            onKeyDown={(e) => {
                              if (e.key === 'Escape') cancelEdit();
                              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                                if (entry.ts) submitEdit(entry.ts);
                              }
                            }}
                          />
                          <div className="flex items-center justify-between gap-2 px-4 py-2 border-t border-[hsl(var(--code-border))]">
                            <span className="inline-flex items-start gap-1.5 text-2xs leading-snug text-[hsl(var(--muted-foreground))] min-w-0">
                              <Info className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--warning)/0.85)]" />
                              <span>
                                Regenerating re-runs from here. Prior agent actions{' '}
                                <em className="not-italic font-medium text-[hsl(var(--code-fg))]">
                                  (file edits, commands)
                                </em>{' '}
                                are not reverted.
                              </span>
                            </span>
                            <span className="flex items-center gap-2 shrink-0">
                              <Button variant="ghost" size="sm" onClick={cancelEdit}>
                                Cancel
                              </Button>
                              <Button
                                variant="primary"
                                size="sm"
                                onClick={() => {
                                  if (entry.ts) submitEdit(entry.ts);
                                }}
                              >
                                Save &amp; regenerate
                              </Button>
                            </span>
                          </div>
                        </div>
                      </div>
                    ) : (
                      <>
                        {entry.attachments && entry.attachments.length > 0 && (
                          <AttachmentCards attachments={entry.attachments} />
                        )}
                        <MessageBubble role={entry.role} content={entry.content} />
                      </>
                    )}
                    {/* Hover-revealed row actions (Copy, Edit). Idle opacity 0,
                        full when the row is hovered or focused. Slight lift on
                        reveal to read as deliberately surfaced. */}
                    <div className="mt-2 inline-flex items-center gap-3 opacity-0 -translate-y-0.5 transition-all duration-150 group-hover/turn:opacity-100 group-hover/turn:translate-y-0 focus-within:opacity-100 focus-within:translate-y-0">
                      <CopyButton
                        text={entry.content}
                        className="group/btn inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/40 transition-colors shrink-0"
                      >
                        <span className="text-2xs">Copy</span>
                      </CopyButton>
                      {!isRunning && editingId !== entry.id && onEditAndRegenerate && (
                        <button
                          onClick={() => startEdit(entry.id, entry.content)}
                          className="group/btn inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/40 transition-colors"
                          aria-label="Edit and regenerate"
                        >
                          <Pencil className="w-3 h-3" />
                          <span className="text-2xs">Edit</span>
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
              {showPending &&
                (streamingText ? (
                  <StreamingAssistantRow
                    text={streamingText}
                    onShowTrace={onShowActiveTrace}
                    stepStartTs={activeStepStartTs}
                    phase={activePhase}
                    tokPerSec={activeTokPerSec}
                  />
                ) : (
                  <PendingAssistantRow
                    onShowTrace={onShowActiveTrace}
                    stepStartTs={activeStepStartTs}
                    phase={activePhase}
                    tokPerSec={activeTokPerSec}
                  />
                ))}
              </Fragment>
            );
          }

          // Assistant + system-as-assistant fallthrough
          const isLatestAssistant = idx === latestAssistantIdx;
          // The footer owns copy for a real assistant turn, so the bubble must
          // not render its own copy affordance too (one turn, one copy).
          const hasFooter = entry.role === 'assistant' && Boolean(entry.turn);
          return (
            <div {...rowProps}>
              <SmartCollapse isLatest={isLatestAssistant}>
                <MessageBubble role={entry.role} content={entry.content} showCopy={!hasFooter}>
                  {entry.role === 'assistant' && entry.turn && entry.turn.files.length > 0 && (
                    <div className="mt-3">
                      <FileList
                        files={entry.turn.files}
                        onFileClick={(file) => onOpenFiles(entry.turn as TurnMeta, file)}
                      />
                    </div>
                  )}
                </MessageBubble>
              </SmartCollapse>
              {entry.role === 'assistant' && entry.turn && (
                <AssistantTurnFooter
                  turn={entry.turn}
                  model={model}
                  sessionUsage={sessionUsage}
                  onShowTrace={onShowTrace}
                  onOpenFiles={(t) => onOpenFiles(t)}
                  onRetryFrom={onRetryFrom}
                  onForkFrom={onForkFrom}
                  onForkSession={onForkSession}
                  responseText={entry.content}
                  isRunning={isRunning}
                />
              )}
            </div>
          );
        })}

        {isStarting && <StartingRunRow />}

        {systemBlock && (
          <div className="pt-6">
            <MessageBubble role="system">
              <div className="mt-2">
                {systemBlock.summary && (
                  <SummaryBlock
                    summary={systemBlock.summary.text}
                    testing={systemBlock.summary.testing}
                  />
                )}
              </div>
            </MessageBubble>
          </div>
        )}
        </div>
      </div>
      {!isAtBottom && <ScrollToBottom onClick={scrollToBottom} isRunning={isRunning} />}
    </div>
  );
}
