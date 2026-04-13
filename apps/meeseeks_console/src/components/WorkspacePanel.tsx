import { X, Maximize2, Minimize2 } from 'lucide-react';
import { ReviewPane } from './ReviewPane';
import { LogsView } from './LogsView';
import { DiffFile, EventRecord, TurnMeta } from '../types';
import { cn } from '../utils/cn';
import { Button } from './ui/button';
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
  isMaximized?: boolean;
  onToggleMaximize?: () => void;
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
  isMaximized,
  onToggleMaximize
}: WorkspacePanelProps) {
  return (
    <div className={cn(
      "flex flex-col h-full bg-[hsl(var(--surface))]",
      !isMaximized && "border-l border-[hsl(var(--border-strong))]"
    )}>
      <div className="flex items-center justify-between px-4 h-10 border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--surface))]">
        <div className="flex gap-6 h-full">
          <button
            onClick={() => onTabChange('diff')}
            className={`h-full text-sm font-medium border-b-2 transition-colors px-1 ${activeTab === 'diff' ? 'border-[hsl(var(--foreground))] text-[hsl(var(--foreground))]' : 'border-transparent text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

            Diff
          </button>
          <button
            onClick={() => onTabChange('logs')}
            className={`h-full text-sm font-medium border-b-2 transition-colors px-1 ${activeTab === 'logs' ? 'border-[hsl(var(--foreground))] text-[hsl(var(--foreground))]' : 'border-transparent text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'}`}>

            Logs
          </button>
        </div>
        <div className="flex items-center gap-1 text-[hsl(var(--muted-foreground))]">
          {onToggleMaximize && (
            <Button
              variant="ghost"
              size="sm"
              iconOnly
              onClick={onToggleMaximize}
              aria-label={isMaximized ? "Minimize panel" : "Maximize panel"}>
              {isMaximized ? <Minimize2 className="w-4 h-4" /> : <Maximize2 className="w-4 h-4" />}
            </Button>
          )}
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={onClose}
            aria-label="Close panel">
            <X className="w-4 h-4" />
          </Button>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">
        {activeTab === 'diff' ?
        <ReviewPane sessionId={sessionId} selectedTurn={selectedTurn} sessionFiles={sessionFiles} /> :

        <LogsView events={events} onRetry={onRetry} onContinue={onContinue} />
        }
      </div>
    </div>);

}