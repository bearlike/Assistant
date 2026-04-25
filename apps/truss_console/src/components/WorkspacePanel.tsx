import { useLayoutEffect, useRef, useState } from 'react';
import { X, Maximize2, Minimize2, GitCompare, ScrollText } from 'lucide-react';
import { ReviewPane } from './ReviewPane';
import { LogsView } from './LogsView';
import { DiffFile, EventRecord, TurnMeta } from '../types';
import { cn } from '../utils/cn';
import { Button } from './ui/button';
import type { RunStatus } from './InputBar';

interface WorkspacePanelProps {
  onClose: () => void;
  activeTab: 'diff' | 'logs';
  onTabChange: (tab: 'diff' | 'logs') => void;
  events: EventRecord[];
  sessionId: string;
  selectedTurn: TurnMeta | null;
  sessionFiles: DiffFile[];
  onRetry?: () => void;
  onContinue?: () => void;
  isRunning?: boolean;
  /** Live run telemetry forwarded to the LogsView FlowerSpinner. */
  runStatus?: RunStatus;
  /** True when the events shown belong to the currently-running turn (or
   *  when there is no specific selection and the rolling stream is live). */
  isViewingLive?: boolean;
  /** Jump-to-live handler — present only when there's a live turn to jump to. */
  onShowLiveTrace?: () => void;
  isMaximized?: boolean;
  onToggleMaximize?: () => void;
}

interface TabDef {
  id: 'diff' | 'logs';
  label: string;
  icon: typeof GitCompare;
  count: number;
}

export function WorkspacePanel({
  onClose,
  activeTab,
  onTabChange,
  events,
  sessionId,
  selectedTurn,
  sessionFiles,
  onRetry,
  onContinue,
  isRunning,
  runStatus,
  isViewingLive,
  onShowLiveTrace,
  isMaximized,
  onToggleMaximize,
}: WorkspacePanelProps) {
  const tabs: TabDef[] = [
    { id: 'diff', label: 'Diff', icon: GitCompare, count: sessionFiles.length },
    { id: 'logs', label: 'Logs', icon: ScrollText, count: events.length },
  ];

  // Sliding under-line indicator — measures the active tab's bounding box
  // and animates left/width via CSS transitions on the absolute span. Co-
  // located here per E4/E6: state lives where it's used; no <TabIndicator>
  // wrapper for an 8-line measurement.
  const tabStripRef = useRef<HTMLDivElement | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [indicator, setIndicator] = useState<{ left: number; width: number } | null>(null);

  useLayoutEffect(() => {
    const strip = tabStripRef.current;
    const btn = tabRefs.current[activeTab];
    if (!strip || !btn) return;
    const sRect = strip.getBoundingClientRect();
    const bRect = btn.getBoundingClientRect();
    setIndicator({ left: bRect.left - sRect.left, width: bRect.width });
  }, [activeTab, tabs.length]);

  return (
    <div
      className={cn(
        // Workspace = "instrument panel". Tonal step deeper than the conversation
        // pane (which sits on --background). Body deepens further to --surface-deep
        // so log cards (which use --card) visibly lift off the surface.
        'flex flex-col h-full bg-[hsl(var(--surface))]',
        !isMaximized && 'border-l border-[hsl(var(--border-strong))]',
      )}
    >
      <div className="flex items-center justify-between pl-1 pr-2 h-11 border-b border-[hsl(var(--border))] bg-[hsl(var(--code-chrome))]">
        <div ref={tabStripRef} className="relative flex h-full items-stretch">
          {tabs.map((t) => {
            const Icon = t.icon;
            const active = activeTab === t.id;
            return (
              <button
                key={t.id}
                ref={(el) => (tabRefs.current[t.id] = el)}
                onClick={() => onTabChange(t.id)}
                className={cn(
                  'group inline-flex items-center gap-1.5 px-3 h-full text-sm font-medium transition-colors',
                  active
                    ? 'text-[hsl(var(--foreground))]'
                    : 'text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]',
                )}
                aria-current={active ? 'page' : undefined}
              >
                <Icon className="h-3.5 w-3.5" aria-hidden />
                <span>{t.label}</span>
                {t.count > 0 && (
                  <span
                    className={cn(
                      'inline-flex items-center justify-center text-[10px] font-mono leading-none px-1.5 py-0.5 rounded-[4px] tabular-nums',
                      active
                        ? 'bg-[hsl(var(--primary))]/15 text-[hsl(var(--primary))]'
                        : 'bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]',
                    )}
                  >
                    {t.count}
                  </span>
                )}
              </button>
            );
          })}
          {indicator && (
            <span
              className="tab-ind"
              style={{ left: indicator.left, width: indicator.width }}
              aria-hidden
            />
          )}
        </div>
        <div className="flex items-center gap-1 text-[hsl(var(--muted-foreground))]">
          {onToggleMaximize && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={onToggleMaximize}
              aria-label={isMaximized ? 'Minimize panel' : 'Maximize panel'}
            >
              {isMaximized ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={onClose}
            aria-label="Close panel"
          >
            <X className="w-4 h-4" />
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-hidden bg-[hsl(var(--surface-deep))]">
        {activeTab === 'diff' ? (
          <ReviewPane sessionId={sessionId} selectedTurn={selectedTurn} sessionFiles={sessionFiles} />
        ) : (
          <LogsView
            events={events}
            onRetry={onRetry}
            onContinue={onContinue}
            isRunning={isRunning}
            runStatus={runStatus}
            isViewingLive={isViewingLive}
            onShowLiveTrace={onShowLiveTrace}
          />
        )}
      </div>
    </div>
  );
}
