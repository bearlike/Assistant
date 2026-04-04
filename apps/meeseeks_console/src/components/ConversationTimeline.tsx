import { useMemo } from 'react';
import { ChevronRight, Loader2 } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import { CopyButton } from './CopyButton';
import { ScrollToBottom } from './ScrollToBottom';
import { DiffFile, EventRecord, TimelineEntry, TurnMeta } from '../types';
import { FileList } from './FileList';
import { SummaryBlock } from './SummaryBlock';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { formatModelName } from '../utils/model';
interface ConversationTimelineProps {
  timeline: TimelineEntry[];
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta, file?: DiffFile) => void;
  activeTurnId?: string | null;
  isRunning?: boolean;
  onShowActiveTrace?: () => void;
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
  events = [],
  model,
  systemBlock
}: ConversationTimelineProps) {
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
    <div ref={scrollRef} onScroll={onScroll} className="h-full overflow-y-auto p-6 space-y-10">
      {timeline.map((entry) =>
      <div key={entry.id} className="flex flex-col gap-2">
          {entry.role === 'user' ?
        <div className="flex justify-end">
              <div className="inline-flex flex-col items-end max-w-[85%]">
                <MessageBubble role={entry.role} content={entry.content} />
                <div className="mt-2 inline-flex items-center gap-3">
                  <CopyButton
                    text={entry.content}
                    className="group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors shrink-0"
                  >
                    <span className="hidden text-[10px] group-hover:inline-block">Copy</span>
                  </CopyButton>
                  {entry.turnId === activeTurnId &&
              <div className="inline-flex flex-col text-xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-lg overflow-hidden max-w-[320px]">
                      <div className="flex items-center justify-between gap-3 px-3 py-1.5">
                        <div className="flex items-center gap-2 min-w-0">
                          {isRunning &&
                    <Loader2 className="w-3.5 h-3.5 animate-spin shrink-0" />
                          }
                          <span className="whitespace-nowrap">{isRunning ? 'Running...' : 'Trace available'}</span>
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
                              {a.model && <span className="opacity-50 shrink-0">{formatModelName(a.model)}</span>}
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
          content={entry.content}>

              {entry.role === 'assistant' && entry.turn &&
          <div className="mt-5 pt-4 border-t border-[hsl(var(--border))] space-y-4">
                  <div className="flex items-center gap-4 text-xs text-[hsl(var(--muted-foreground))]">
                    {model &&
              <span className="font-mono opacity-70">
                        {formatModelName(model)}
                      </span>
              }
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
