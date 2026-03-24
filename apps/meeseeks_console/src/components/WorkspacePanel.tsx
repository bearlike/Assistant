import { X, Maximize2 } from 'lucide-react';
import { DiffView } from './DiffView';
import { LogsView } from './LogsView';
import { EventRecord } from '../types';
interface WorkspacePanelProps {
  onClose: () => void;
  activeTab: 'diff' | 'logs';
  onTabChange: (tab: 'diff' | 'logs') => void;
  events: EventRecord[];
  diffContent?: string;
  filename?: string;
}
export function WorkspacePanel({
  onClose,
  activeTab,
  onTabChange,
  events,
  diffContent,
  filename
}: WorkspacePanelProps) {
  return (
    <div className="flex flex-col h-full border-l border-[hsl(var(--border-strong))] bg-[hsl(var(--background))]">
      <div className="flex items-center justify-between px-4 h-10 border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--background))]">
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
        <div className="flex items-center gap-3 text-[hsl(var(--muted-foreground))]">
          <button className="hover:text-[hsl(var(--foreground))] transition-colors">
            <Maximize2 className="w-4 h-4" />
          </button>
          <button
            onClick={onClose}
            className="hover:text-[hsl(var(--foreground))] transition-colors">

            <X className="w-4 h-4" />
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">
        {activeTab === 'diff' ?
        <DiffView diffContent={diffContent} filename={filename} /> :

        <LogsView events={events} />
        }
      </div>
    </div>);

}