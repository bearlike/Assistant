import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, FolderOpen, RefreshCw } from 'lucide-react';
import { ProjectSummary } from '../api/client';
import { Popover } from './Popover';

type ProjectSelectorProps = {
  projects: ProjectSummary[];
  activeProject: string | null;
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  direction?: 'up' | 'down';
  compact?: boolean;
  onRefresh?: () => void;
  onToggleOpen: () => void;
  onSelect: (name: string | null) => void;
};

export const ProjectSelector = forwardRef<HTMLDivElement, ProjectSelectorProps>(
  (
    { projects, activeProject, isOpen, loading = false, error, direction = 'up', compact = false, onRefresh, onToggleOpen, onSelect },
    ref
  ) => {
    if (projects.length === 0 && !loading) {
      return null;
    }
    const label = activeProject ? activeProject : 'Project';
    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select project"
          className={`flex items-center gap-1.5 ${compact ? 'p-1.5' : 'px-2.5 py-1.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeProject ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <FolderOpen className={`w-3 h-3 ${activeProject ? 'text-blue-500' : 'opacity-50'}`} />
          {!compact && (
            <>
              <span>{loading ? 'Loading...' : label}</span>
              {isOpen ? (
                <ChevronUp className="w-3 h-3 opacity-50" />
              ) : (
                <ChevronDown className="w-3 h-3 opacity-50" />
              )}
            </>
          )}
        </button>

        {isOpen && (
          <Popover direction={direction} width="w-64" maxHeight="max-h-[320px]">
            <div className="flex items-center justify-between px-3 pt-2 pb-1">
              <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
                Projects
              </span>
              {onRefresh && (
                <button
                  onClick={onRefresh}
                  aria-label="Refresh projects"
                  className="p-0.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                >
                  <RefreshCw className="w-3 h-3" />
                </button>
              )}
            </div>
            {error && (
              <div className="px-3 py-2 text-xs text-red-500 border-b border-[hsl(var(--border))] bg-red-500/5">
                {error}
              </div>
            )}
            <div className="py-1">
              <button
                onClick={() => onSelect(null)}
                className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeProject ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
              >
                <span>Default (server CWD)</span>
              </button>
              {projects.map((project) => (
                <button
                  key={project.name}
                  onClick={() => onSelect(project.name)}
                  className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${activeProject === project.name ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--foreground))]'}`}
                >
                  <span>{project.name}</span>
                  {project.description && (
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {project.description}
                    </span>
                  )}
                </button>
              ))}
            </div>
          </Popover>
        )}
      </div>
    );
  }
);
ProjectSelector.displayName = 'ProjectSelector';
