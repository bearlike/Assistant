import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, Cpu, RefreshCw } from 'lucide-react';
import { Popover } from './Popover';

type ModelSelectorProps = {
  models: string[];
  activeModel: string | null;
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  direction?: 'up' | 'down';
  compact?: boolean;
  onRefresh?: () => void;
  onToggleOpen: () => void;
  onSelect: (name: string | null) => void;
};

/** Truncate a model ID for display (e.g. "openai/gpt-4o-mini" → "gpt-4o-mini"). */
function shortModel(id: string): string {
  const slash = id.lastIndexOf('/');
  return slash >= 0 ? id.slice(slash + 1) : id;
}

export const ModelSelector = forwardRef<HTMLDivElement, ModelSelectorProps>(
  (
    { models, activeModel, isOpen, loading = false, error, direction = 'up', compact = false, onRefresh, onToggleOpen, onSelect },
    ref
  ) => {
    if (models.length === 0 && !loading) {
      return null;
    }
    const label = activeModel ? shortModel(activeModel) : 'Model';
    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select model"
          className={`flex items-center gap-1.5 ${compact ? 'p-1.5' : 'px-2.5 py-1.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeModel ? 'text-[hsl(var(--foreground))]' : ''}`}
        >
          <Cpu className={`w-3 h-3 ${activeModel ? 'text-purple-500' : 'opacity-50'}`} />
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
          <Popover direction={direction} width="w-56">
            <div className="flex items-center justify-between px-3 pt-2 pb-1">
              <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
                Models
              </span>
              {onRefresh && (
                <button
                  onClick={onRefresh}
                  aria-label="Refresh models"
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
                className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${!activeModel ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--muted-foreground))]'}`}
              >
                <span>Default</span>
              </button>
              {models.map((model) => (
                <button
                  key={model}
                  onClick={() => onSelect(model)}
                  className={`w-full flex flex-col items-start px-3 py-2 text-xs text-left transition-colors hover:bg-[hsl(var(--accent))] ${activeModel === model ? 'text-[hsl(var(--foreground))] font-medium' : 'text-[hsl(var(--foreground))]'}`}
                >
                  <span>{shortModel(model)}</span>
                  {model.includes('/') && (
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {model}
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
ModelSelector.displayName = 'ModelSelector';
