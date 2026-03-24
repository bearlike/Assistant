import { useRef, useState, useEffect } from 'react';
import { ShellBlock } from './ShellBlock';
import { SummaryBlock } from './SummaryBlock';
import { EventRecord } from '../types';
import { buildLogs, extractSummaryTesting } from '../utils/logs';
import { formatSessionTime } from '../utils/time';
export function LogsView({ events }: {events: EventRecord[];}) {
  const logs = buildLogs(events);
  const summaryData = extractSummaryTesting(events);
  const hasSummary =
    summaryData.summary.length > 0 || summaryData.testing.length > 0;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);

  const scrollToBottom = () => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    container.scrollTop = container.scrollHeight;
  };

  useEffect(() => {
    if (isAtBottom) {
      scrollToBottom();
    }
  }, [events.length, isAtBottom]);

  const handleScroll = () => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    const threshold = 64;
    const atBottom =
      container.scrollTop + container.clientHeight >=
      container.scrollHeight - threshold;
    setIsAtBottom(atBottom);
  };
  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto bg-[hsl(var(--background))] p-6 space-y-6">
        {logs.map((log) => {
          if (log.type === 'plan') {
            return (
              <div
                key={log.id}
                className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4">

                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold text-[hsl(var(--foreground))]">
                      {log.label || 'Plan'}
                    </h3>
                    {log.version && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
                        v{log.version}
                      </span>
                    )}
                    {log.planMode === 'diff' && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))]">
                        diff
                      </span>
                    )}
                  </div>
                  {log.timestamp && (
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                      {formatSessionTime(log.timestamp)}
                    </span>
                  )}
                </div>

                <ol className="space-y-3">
                  {(log.steps || []).map((step, idx) => (
                    <li key={idx} className="flex gap-3">
                      <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] mt-0.5">
                        {idx + 1}.
                      </span>
                      <div>
                        <div className="flex items-center gap-2 text-sm text-[hsl(var(--foreground))] font-medium">
                          {step.title}
                          {step.diffType && (
                            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
                              {step.diffType === 'added' ? 'Added' : step.diffType === 'removed' ? 'Removed' : 'Updated'}
                            </span>
                          )}
                        </div>
                        {step.description && (
                          <div className="text-xs text-[hsl(var(--muted-foreground))] mt-1 leading-relaxed">
                            {step.description}
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              </div>);

          }
          if (log.type === 'shell') {
            return (
              <ShellBlock key={log.id} content={log.content} title={log.title} />);
          }
          return (
            <div
              key={log.id}
              className="text-sm text-[hsl(var(--muted-foreground))] pl-1">
              {log.content}
            </div>);
        })}

        {hasSummary && (
          <div className="pt-8 border-t border-[hsl(var(--border))] mt-8">
            <h3 className="text-sm font-medium text-[hsl(var(--foreground))] mb-4">
              Preparing pull request
            </h3>
            <SummaryBlock
              summary={summaryData.summary}
              testing={summaryData.testing} />
          </div>
        )}
      </div>
      {!isAtBottom && (
        <button
          type="button"
          onClick={() => {
            scrollToBottom();
            setIsAtBottom(true);
          }}
          aria-label="Jump to latest logs"
          className="absolute bottom-4 right-4 h-10 w-10 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[hsl(var(--foreground))] shadow-md transition hover:-translate-y-0.5">
          <svg
            viewBox="0 0 24 24"
            aria-hidden="true"
            className="mx-auto h-5 w-5">
            <path
              fill="currentColor"
              d="M12 16.5 5 9.5l1.4-1.4L12 13.7l5.6-5.6L19 9.5z" />
          </svg>
        </button>
      )}
    </div>);

}
