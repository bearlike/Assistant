import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, Zap } from 'lucide-react';
import { SkillSummary } from '../api/client';

type SkillSelectorProps = {
  skills: SkillSummary[];
  activeSkill: string | null;
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  onToggleOpen: () => void;
  onSelect: (name: string | null) => void;
};

export const SkillSelector = forwardRef<HTMLDivElement, SkillSelectorProps>(
  (
    { skills, activeSkill, isOpen, loading = false, error, onToggleOpen, onSelect },
    ref
  ) => {
    const label = activeSkill ? activeSkill : 'Skills';
    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select skill"
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeSkill ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <Zap className={`w-3 h-3 ${activeSkill ? 'text-amber-500' : 'opacity-50'}`} />
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
          </div>
        )}
      </div>
    );
  }
);
SkillSelector.displayName = 'SkillSelector';
