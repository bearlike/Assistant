import { useCallback, useMemo, useState } from 'react';
import { ChevronRight, Info, Loader2, Pencil } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import { CopyButton } from './CopyButton';
import { ScrollToBottom } from './ScrollToBottom';
import { DiffFile, EventRecord, TimelineEntry, TurnMeta } from '../types';
import { FileList } from './FileList';
import { PlanCard } from './PlanCard';
import { SummaryBlock } from './SummaryBlock';
import { RetryFromHereButton } from './RetryFromHereButton';
import { ForkFromHereButton } from './ForkFromHereButton';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { ModelLabel } from './ModelLabel';
import { Button } from './ui/Button';
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
  events?: EventRecord[];
  model?: string;
  systemBlock?: {
    summary?: {
      text: string[];
      testing: {
        command: string;
        passed: boolean;
      }[];
    };
  };
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
  events = [],
  model,
  systemBlock
}: ConversationTimelineProps) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  const startEdit = useCallback((id: string, content: string) => {
    setEditingId(id);
    setEditText(content);
  }, []);

  const submitEdit = useCallback((fromTs: string) => {
    if (!editText.trim() || !onEditAndRegenerate) return;
    onEditAndRegenerate(fromTs, editText.trim());
    setEditingId(null);
    setEditText('');
  }, [editText, onEditAndRegenerate]);

  const cancelEdit = useCallback(() => {
    setEditingId(null);
    setEditText('');
  }, []);
  const awaitingPlanApproval = useMemo(() => {
    if (timeline.length === 0) return false;
    const last = timeline[timeline.length - 1];
    return last.role === 'plan' && last.plan?.status === 'pending';
  }, [timeline]);
  const activeAgents = useMemo(() => {
    const agents = new Map<string, { detail: string; model: string }>();
    for (const event of events) {
      if (event.type === 'sub_agent') {
        const p = event.payload as Record<string, unknown>;
        const id = ((p.agent_id as string) ?? '').slice(0, 8);
        if (p.action === 'start') {
          agents.set(id, {
            detail: ((p.detail as string) ?? '').slice(0, 60),
            model: (p.model as string) ?? '',
          });
        } else if (p.action === 'stop') {
          agents.delete(id);
        }
      }
    }
    return agents;
  }, [events]);
  const { scrollRef, isAtBottom, scrollToBottom, onScroll } = useAutoScroll(timeline.length);
  return (
    <div className="relative flex-1 overflow-hidden">
    <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-y-auto p-6 pb-24 space-y-10">
      {timeline.map((entry) =>
      <div key={entry.id} className="flex flex-col gap-2">
          {entry.role === 'plan' ?
        entry.plan && <PlanCard plan={entry.plan} onApprove={onApprovePlan} /> :
          entry.role === 'user' ?
        <div className="flex justify-end">
              <div className={`${editingId === entry.id ? 'flex w-full' : 'inline-flex'} flex-col items-end max-w-[85%]`}>
                {editingId === entry.id ? (
                  <div className="w-full space-y-2">
                    <div className="w-full bg-user-msg/80 text-[hsl(var(--card-foreground))] px-4 py-3 rounded-lg text-sm border border-dashed border-[hsl(var(--ring))]/50">
                      <textarea
                        value={editText}
                        onChange={(e) => setEditText(e.target.value)}
                        className="w-full min-h-[80px] bg-transparent text-sm leading-relaxed text-[hsl(var(--card-foreground))] resize-y focus:outline-none"
                        autoFocus
                        onKeyDown={(e) => {
                          if (e.key === 'Escape') cancelEdit();
                          if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
                            const fromTs = entry.turn?.events[0]?.ts;
                            if (fromTs) submitEdit(fromTs);
                          }
                        }}
                      />
                      <div className="flex justify-end gap-2 mt-2 pt-2 border-t border-[hsl(var(--border))]">
                        <Button variant="ghost" size="sm" onClick={cancelEdit}>Cancel</Button>
                        <Button
                          variant="neutral"
                          size="sm"
                          tone="info"
                          onClick={() => {
                            const fromTs = entry.turn?.events[0]?.ts;
                            if (fromTs) submitEdit(fromTs);
                          }}
                        >
                          Save &amp; Regenerate
                        </Button>
                      </div>
                    </div>
                    <div className="flex items-start gap-2 px-1 text-[11px] leading-snug text-[hsl(var(--muted-foreground))]">
                      <Info className="w-3.5 h-3.5 shrink-0 mt-px opacity-60" />
                      <span>Regenerating from here re-runs with your edited prompt. Prior agent actions (file edits, commands) are <strong className="font-medium text-[hsl(var(--foreground))]">not reverted</strong>.</span>
                    </div>
                  </div>
                ) : (
                <MessageBubble role={entry.role} content={entry.content} />
                )}
                <div className="mt-2 inline-flex items-center gap-3">
                  <CopyButton
                    text={entry.content}
                    className="group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors shrink-0"
                  >
                    <span className="hidden text-[10px] group-hover:inline-block">Copy</span>
                  </CopyButton>
                  {!isRunning && editingId !== entry.id && onEditAndRegenerate && (
                    <button
                      onClick={() => startEdit(entry.id, entry.content)}
                      className="group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                      aria-label="Edit and regenerate"
                    >
                      <Pencil className="w-3 h-3" />
                      <span className="hidden text-[10px] group-hover:inline-block">Edit</span>
                    </button>
                  )}
                  {entry.turnId === activeTurnId &&
              <div className="inline-flex flex-col text-xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg overflow-hidden max-w-[320px]">
                      <div className="flex items-center justify-between gap-3 px-3 py-1.5">
                        <div className="flex items-center gap-2 min-w-0">
                          {isRunning && !awaitingPlanApproval &&
                    <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                          }
                          <span className="whitespace-nowrap">{awaitingPlanApproval ? 'Awaiting your approval' : isRunning ? 'Running...' : 'Trace available'}</span>
                          {isRunning && activeAgents.size > 0 &&
                            <span className="whitespace-nowrap opacity-70">{activeAgents.size} agent{activeAgents.size !== 1 ? 's' : ''}</span>
                          }
                        </div>
                        {onShowActiveTrace &&
                  <button
                    onClick={onShowActiveTrace}
                    className="flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors shrink-0">
                          <span>Open trace</span>
                          <ChevronRight className="w-3 h-3 opacity-50" />
                        </button>
                  }
                      </div>
                      {isRunning && activeAgents.size > 0 &&
                        <div className="border-t border-[hsl(var(--border))] px-3 py-1.5 space-y-0.5 bg-[hsl(var(--accent))]/30">
                          {[...activeAgents.entries()].map(([id, a]) => (
                            <div key={id} className="flex items-center gap-1.5 text-[10px] text-[hsl(var(--muted-foreground))]">
                              <span className="font-mono shrink-0">{id}</span>
                              <ModelLabel modelId={a.model} className="opacity-50 shrink-0" />
                              <span className="truncate opacity-70">{a.detail}</span>
                            </div>
                          ))}
                        </div>
                      }
                    </div>
                  }
                </div>
              </div>
            </div> :

        <MessageBubble
          role={entry.role}
          content={entry.content}
          actions={(() => {
            const fromTs = entry.turn?.events[0]?.ts;
            if (isRunning || !fromTs) return undefined;
            const actionClass = "group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors";
            return (
              <div className="inline-flex items-center gap-3">
                {onRetryFrom && (
                  <RetryFromHereButton
                    onConfirm={() => onRetryFrom(fromTs)}
                    className={actionClass}
                  />
                )}
                {onForkFrom && (
                  <ForkFromHereButton
                    onConfirm={() => onForkFrom(fromTs)}
                    className={actionClass}
                  />
                )}
              </div>
            );
          })()}>

              {entry.role === 'assistant' && entry.turn &&
          <div className="mt-5 pt-4 border-t border-[hsl(var(--border))] space-y-4">
                  <div className="flex items-center gap-4 text-xs text-[hsl(var(--muted-foreground))]">
                    <ModelLabel modelId={entry.turn?.model ?? model} className="font-mono opacity-70" />
                    {entry.turn.duration &&
              <span className="font-mono opacity-70">
                        {entry.turn.duration}
                      </span>
              }
                    <div className="h-3 w-px bg-[hsl(var(--border))]" />
                    <button
                onClick={() => onShowTrace(entry.turn as TurnMeta)}
                className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                      <span>Show Trace</span>
                      <ChevronRight className="w-3 h-3 opacity-50" />
                    </button>
                    {entry.turn.files.length > 0 &&
              <>
                        <div className="h-3 w-px bg-[hsl(var(--border))]" />
                        <button
                  onClick={() => onOpenFiles(entry.turn as TurnMeta)}
                  className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                          <span>Open files</span>
                          <ChevronRight className="w-3 h-3 opacity-50" />
                        </button>
                      </>
              }
                  </div>
                  {entry.turn.files.length > 0 &&
            <FileList
              files={entry.turn.files}
              onFileClick={(file) =>
              onOpenFiles(entry.turn as TurnMeta, file)
              } />

            }
                </div>
              }
            </MessageBubble>
          }
        </div>
      )}

      {systemBlock &&
      <MessageBubble role="system">
          <div className="mt-2">
            {systemBlock.summary &&
          <SummaryBlock
            summary={systemBlock.summary.text}
            testing={systemBlock.summary.testing} />

          }
          </div>
        </MessageBubble>
      }
    </div>
    {!isAtBottom && (
      <ScrollToBottom onClick={scrollToBottom} />
    )}
    </div>);

}
