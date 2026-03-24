import React, { useMemo } from 'react';
import { ChevronRight, Loader2, Copy } from 'lucide-react';
import { MessageBubble } from './MessageBubble';
import { DiffFile, EventRecord, TimelineEntry, TurnMeta } from '../types';
import { FileList } from './FileList';
import { SummaryBlock } from './SummaryBlock';
import { copyText } from '../utils/clipboard';
interface ConversationTimelineProps {
  timeline: TimelineEntry[];
  onShowTrace: (turn: TurnMeta) => void;
  onOpenFiles: (turn: TurnMeta, file?: DiffFile) => void;
  activeTurnId?: string | null;
  isRunning?: boolean;
  onShowActiveTrace?: () => void;
  events?: EventRecord[];
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
  systemBlock
}: ConversationTimelineProps) {
  const activeAgentCount = useMemo(() => {
    let count = 0;
    for (const event of events) {
      if (event.type === 'sub_agent') {
        const action = (event.payload as Record<string, unknown>)?.action;
        if (action === 'start') {
          count++;
        } else if (action === 'stop') {
          count--;
        }
      }
    }
    return Math.max(0, count);
  }, [events]);
  return (
    <div className="flex-1 overflow-y-auto p-6 space-y-10">
      {timeline.map((entry) =>
      <div key={entry.id} className="flex flex-col gap-2">
          {entry.role === 'user' ?
        <div className="flex justify-end">
              <div className="inline-flex flex-col items-end max-w-[70%]">
                <MessageBubble role={entry.role} content={entry.content} />
                <div className="mt-2 inline-flex items-center gap-3">
                  <button
                  onClick={() => copyText(entry.content)}
                  className="group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors shrink-0">

                    <Copy className="w-3 h-3" />
                    <span className="hidden text-[10px] group-hover:inline-block">
                      Copy
                    </span>
                  </button>
                  {entry.turnId === activeTurnId &&
              <div className="flex items-center justify-between gap-3 text-xs text-[hsl(var(--muted-foreground))] bg-[hsl(var(--card))] border border-[hsl(var(--border))] rounded-full px-3 py-1 whitespace-nowrap">
                      <div className="flex items-center gap-2">
                        {isRunning &&
                    <Loader2 className="w-3.5 h-3.5 animate-spin" />
                        }
                        <span>{isRunning ? 'Running...' : 'Trace available'}</span>
                        {isRunning && activeAgentCount > 0 &&
                          <span className="text-[10px] font-medium text-[hsl(var(--primary))] bg-[hsl(var(--primary))]/10 border border-[hsl(var(--primary))]/30 px-1.5 py-0.5 rounded-full">
                            {activeAgentCount} agent{activeAgentCount !== 1 ? 's' : ''}
                          </span>
                        }
                      </div>
                      {onShowActiveTrace &&
                  <button
                    onClick={onShowActiveTrace}
                    className="flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                          <span>Open trace</span>
                          <ChevronRight className="w-3 h-3 opacity-50" />
                        </button>
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
    </div>);

}
