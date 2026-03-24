import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, FolderOpen } from 'lucide-react';
import { ProjectSummary } from '../api/client';

type ProjectSelectorProps = {
  projects: ProjectSummary[];
  activeProject: string | null;
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  onToggleOpen: () => void;
  onSelect: (name: string | null) => void;
};

export const ProjectSelector = forwardRef<HTMLDivElement, ProjectSelectorProps>(
  (
    { projects, activeProject, isOpen, loading = false, error, onToggleOpen, onSelect },
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
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeProject ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <FolderOpen className={`w-3 h-3 ${activeProject ? 'text-blue-500' : 'opacity-50'}`} />
          <span>{loading ? 'Loading...' : label}</span>
          {isOpen ? (
            <ChevronUp className="w-3 h-3 opacity-50" />
          ) : (
            <ChevronDown className="w-3 h-3 opacity-50" />
          )}
        </button>

        {isOpen && (
          <div className="absolute bottom-full left-0 mb-2 w-64 bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-2xl shadow-black/40 ring-1 ring-white/[0.03] overflow-hidden z-50 max-h-[320px] overflow-y-auto text-left flex flex-col">
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
          </div>
        )}
      </div>
    );
  }
);
ProjectSelector.displayName = 'ProjectSelector';
