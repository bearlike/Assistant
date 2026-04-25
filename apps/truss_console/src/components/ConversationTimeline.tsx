import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, ChevronDown, Info, MoreHorizontal, Pencil } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import { WidgetCard } from './WidgetCard';
import { CopyButton } from './CopyButton';
import { ScrollToBottom } from './ScrollToBottom';
import { TurnScroller } from './TurnScroller';
import { copyText } from '../utils/clipboard';
import { DiffFile, SessionUsage, TimelineEntry, TurnMeta } from '../types';
import { FileList } from './FileList';
import { PlanCard } from './PlanCard';
import { SummaryBlock } from './SummaryBlock';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { ModelLabel } from './ModelLabel';
import { Button } from './ui/button';
import { ContextWindowBar } from './ContextWindowBar';
import { formatTokens } from '../utils/time';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from './ui/dropdown-menu';

interface ConversationTimelineProps {
  timeline: TimelineEntry[];
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta, file?: DiffFile) => void;
  activeTurnId?: string | null;
  isRunning?: boolean;
  onShowActiveTrace?: () => void;
  onApprovePlan?: (approved: boolean) => void;
  onRetryFrom?: (fromTs: string) => void;
  onForkFrom?: (fromTs: string) => void;
  onEditAndRegenerate?: (fromTs: string, newText: string) => void;
  model?: string;
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
 * Inline beat shown immediately after a user row while the agent has
 * accepted the turn but hasn't streamed a single token yet. Replaces the
 * old bordered "Working… / Open trace" pill — visually one continuous
 * beat with the user bubble, not a separate card.
 */
function PendingAssistantRow({ onShowTrace }: { onShowTrace?: () => void }) {
  return (
    <div className="pt-1">
      <div className="pending-line" role="status" aria-live="polite">
        <span className="pending-dot" aria-hidden />
        <span className="pending-label">Working</span>
        {onShowTrace && (
          <button
            type="button"
            className="pending-trace"
            onClick={onShowTrace}
            title="Open trace"
          >
            <Activity className="w-2.5 h-2.5" aria-hidden />
            <span>Trace</span>
          </button>
        )}
      </div>
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
 * Per-turn footer for assistant messages.
 *
 * Layout: model · duration on the left (small mono meta), primary "Trace"
 * action and a 3-dot overflow menu on the right. Hover/focus-revealed —
 * idle opacity 0.4, full opacity when the parent row is hovered or the
 * menu is open. Replaces the previous always-visible inline action strip.
 */
function AssistantTurnFooter({
  turn,
  model,
  sessionUsage,
  onShowTrace,
  onOpenFiles,
  onRetryFrom,
  onForkFrom,
  responseText,
  isRunning,
  isLatest,
}: {
  turn: TurnMeta;
  model?: string;
  sessionUsage?: SessionUsage | null;
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta) => void;
  onRetryFrom?: (fromTs: string) => void;
  onForkFrom?: (fromTs: string) => void;
  responseText: string;
  isRunning?: boolean;
  isLatest?: boolean;
}) {
  const fromTs = turn.events[0]?.ts;
  const usage = turn.tokenUsage;
  const tokensLine = useMemo(() => {
    if (!usage) return null;
    const peak = Math.max(usage.inputTokens, usage.subInputTokens);
    return peak > 0 ? `${formatTokens(peak)} in · ${formatTokens(usage.outputTokens)} out` : null;
  }, [usage]);

  return (
    <div
      className={`mt-4 pt-2 flex items-center justify-between gap-3 text-xs text-[hsl(var(--muted-foreground))] transition-opacity duration-150 group-hover/turn:opacity-100 focus-within:opacity-100 ${
        isLatest ? 'opacity-100' : 'opacity-0'
      }`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 min-w-0">
        <ModelLabel modelId={turn.model ?? model} className="font-mono opacity-80" />
        {turn.duration && (
          <>
            <span className="opacity-40">·</span>
            <span className="font-mono opacity-80">{turn.duration}</span>
          </>
        )}
        {tokensLine && (
          <>
            <span className="opacity-40">·</span>
            <span className="font-mono opacity-70" title="Peak input tokens · output tokens for this turn">
              {tokensLine}
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
      <div className="flex items-center gap-1 shrink-0">
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
              className="h-7 w-7 rounded-md"
            >
              <MoreHorizontal className="h-3.5 w-3.5" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuItem onSelect={() => void copyText(responseText)}>
              Copy response
            </DropdownMenuItem>
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
  onShowActiveTrace,
  onApprovePlan,
  onRetryFrom,
  onForkFrom,
  onEditAndRegenerate,
  model,
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

  const awaitingPlanApproval = useMemo(() => {
    if (timeline.length === 0) return false;
    const last = timeline[timeline.length - 1];
    return last.role === 'plan' && last.plan?.status === 'pending';
  }, [timeline]);

  // Index of the latest assistant entry — only that one defaults expanded.
  const latestAssistantIdx = useMemo(() => {
    for (let i = timeline.length - 1; i >= 0; i--) {
      if (timeline[i].role === 'assistant') return i;
    }
    return -1;
  }, [timeline]);

  const { scrollRef, isAtBottom, scrollToBottom, onScroll } = useAutoScroll(timeline.length);

  return (
    <div className="conv-scroll relative flex-1 overflow-hidden">
      <TurnScroller scrollRef={scrollRef} timeline={timeline} />
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-y-auto"
      >
        <div className="mx-auto w-full max-w-[820px] px-7 pt-6 pb-32">
        {timeline.map((entry, idx) => {
          // Per-row spacing rules:
          //   • Default: ~18px vertical breathing room.
          //   • Assistant follows its own user → tighter top (it's an answer).
          //   • Next user-after-first → turn boundary: bigger top + tiny margin.
          const prev = idx > 0 ? timeline[idx - 1] : undefined;
          let spacing = 'pt-[18px]';
          if (entry.role === 'assistant' && prev?.role === 'user') spacing = 'pt-1.5';
          else if (entry.role === 'user' && idx > 0) spacing = 'pt-8 mt-2';
          else if (entry.role === 'plan' || entry.role === 'widget') spacing = 'pt-3';

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

          if (entry.role === 'plan') {
            return (
              <div {...rowProps}>
                {entry.plan && <PlanCard plan={entry.plan} onApprove={onApprovePlan} />}
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
                            boxShadow:
                              '0 0 0 4px hsl(var(--primary) / 0.10), 0 12px 32px hsl(0 0% 0% / 0.32)',
                          }}
                        >
                          <div className="flex items-center justify-between gap-2 px-4 pt-2.5 pb-1.5">
                            <span className="font-mono text-xs text-[hsl(var(--primary))] inline-flex items-center gap-2">
                              <span aria-hidden>›</span>
                              <span className="text-[hsl(var(--code-fg))]">Editing your message</span>
                            </span>
                            <span className="inline-flex items-center gap-1.5 text-[hsl(var(--muted-foreground))]">
                              <span className="edit-kbd">⌘ ↵</span>
                              <span className="text-[10px]">save</span>
                              <span className="opacity-40">·</span>
                              <span className="edit-kbd">esc</span>
                              <span className="text-[10px]">cancel</span>
                            </span>
                          </div>
                          <textarea
                            value={editText}
                            onChange={(e) => setEditText(e.target.value)}
                            className="w-full min-h-[80px] bg-transparent text-sm leading-relaxed text-[hsl(var(--code-fg))] resize-y focus:outline-none px-4 py-2"
                            autoFocus
                            onKeyDown={(e) => {
                              if (e.key === 'Escape') cancelEdit();
                              if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                                if (entry.ts) submitEdit(entry.ts);
                              }
                            }}
                          />
                          <div className="flex items-center justify-between gap-2 px-4 py-2 border-t border-[hsl(var(--code-border))]">
                            <span className="inline-flex items-start gap-1.5 text-[11px] leading-snug text-[hsl(var(--muted-foreground))] min-w-0">
                              <Info className="w-3.5 h-3.5 shrink-0 mt-0.5 text-amber-500/85" />
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
                      <MessageBubble role={entry.role} content={entry.content} />
                    )}
                    {/* Hover-revealed row actions (Copy, Edit). Idle opacity 0,
                        full when the row is hovered or focused. Slight lift on
                        reveal to read as deliberately surfaced. */}
                    <div className="mt-2 inline-flex items-center gap-3 opacity-0 -translate-y-0.5 transition-all duration-150 group-hover/turn:opacity-100 group-hover/turn:translate-y-0 focus-within:opacity-100 focus-within:translate-y-0">
                      <CopyButton
                        text={entry.content}
                        className="group/btn inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/40 transition-colors shrink-0"
                      >
                        <span className="text-[10px]">Copy</span>
                      </CopyButton>
                      {!isRunning && editingId !== entry.id && onEditAndRegenerate && (
                        <button
                          onClick={() => startEdit(entry.id, entry.content)}
                          className="group/btn inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/40 transition-colors"
                          aria-label="Edit and regenerate"
                        >
                          <Pencil className="w-3 h-3" />
                          <span className="text-[10px]">Edit</span>
                        </button>
                      )}
                    </div>
                  </div>
                </div>
              </div>
              {showPending && <PendingAssistantRow onShowTrace={onShowActiveTrace} />}
              </Fragment>
            );
          }

          // Assistant + system-as-assistant fallthrough
          const isLatestAssistant = idx === latestAssistantIdx;
          return (
            <div {...rowProps}>
              <SmartCollapse isLatest={isLatestAssistant}>
                <MessageBubble role={entry.role} content={entry.content}>
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
                  responseText={entry.content}
                  isRunning={isRunning}
                  isLatest={isLatestAssistant}
                />
              )}
            </div>
          );
        })}

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
