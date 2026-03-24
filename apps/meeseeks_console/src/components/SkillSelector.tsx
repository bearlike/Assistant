import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, RefreshCw, Zap } from 'lucide-react';
import { SkillSummary } from '../api/client';
import { Popover } from './Popover';

type SkillSelectorProps = {
  skills: SkillSummary[];
  activeSkill: string | null;
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  direction?: 'up' | 'down';
  compact?: boolean;
  onToggleOpen: () => void;
  onSelect: (name: string | null) => void;
  onRefresh?: () => void;
};

export const SkillSelector = forwardRef<HTMLDivElement, SkillSelectorProps>(
  (
    { skills, activeSkill, isOpen, loading = false, error, direction = 'up', compact = false, onToggleOpen, onSelect, onRefresh },
    ref
  ) => {
    const label = activeSkill ? activeSkill : 'Skills';
    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select skill"
          className={`flex items-center gap-1.5 ${compact ? 'p-1.5' : 'px-2.5 py-1.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeSkill ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <Zap className={`w-3 h-3 ${activeSkill ? 'text-amber-500' : 'opacity-50'}`} />
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
                Skills
              </span>
              {onRefresh && (
                <button
                  onClick={onRefresh}
                  aria-label="Refresh skills"
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
            {skills.length === 0 && !loading && (
              <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
                No skills available.
              </div>
            )}
            <div className="py-1">
              {/* None option */}
              <button
                onClick={() => onSelect(null)}
                className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeSkill ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
              >
                <span>None</span>
              </button>
              {skills.map((skill) => (
                <button
                  key={skill.name}
                  onClick={() => onSelect(skill.name)}
                  className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${activeSkill === skill.name ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--foreground))]'}`}
                >
                  <span className="flex items-center gap-1.5">
                    <span>/{skill.name}</span>
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1 py-0.5 rounded">
                      {skill.source}
                    </span>
                  </span>
                  <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                    {skill.description}
                  </span>
                </button>
              ))}
            </div>
          </Popover>
        )}
      </div>
    );
  }
);
SkillSelector.displayName = 'SkillSelector';
