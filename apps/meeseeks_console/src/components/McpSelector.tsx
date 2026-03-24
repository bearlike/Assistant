import React, { forwardRef } from 'react';
import { ChevronDown, ChevronUp, Circle } from 'lucide-react';
export type McpOption = {
  id: string;
  name: string;
  active: boolean;
  enabled: boolean;
  count?: number;
};
type McpSelectorProps = {
  options: McpOption[];
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  onToggleOpen: () => void;
  onToggle: (id: string) => void;
};
export const McpSelector = forwardRef<HTMLDivElement, McpSelectorProps>(
  (
  { options, isOpen, loading = false, error, onToggleOpen, onToggle },
  ref) =>
  {
    const activeCount = options.filter((mcp) => mcp.active).length;
    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select MCP tools"
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}>

          <span>{loading ? 'Loading...' : `${activeCount} MCPs`}</span>
          {isOpen ?
          <ChevronUp className="w-3 h-3 opacity-50" /> :

          <ChevronDown className="w-3 h-3 opacity-50" />
          }
        </button>

        {isOpen &&
        <div className="absolute bottom-full left-0 mb-2 w-60 bg-[hsl(var(--popover))] border border-[hsl(var(--border))] rounded-lg shadow-2xl shadow-black/40 ring-1 ring-white/[0.03] overflow-hidden z-50 max-h-[320px] overflow-y-auto text-left flex flex-col">
            {error &&
          <div className="px-3 py-2 text-xs text-red-500 border-b border-[hsl(var(--border))] bg-red-500/5">
                {error}
              </div>
          }
            {options.length === 0 && !loading &&
          <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
                No MCP servers available.
              </div>
          }
            <div className="py-1">
              {options.map((mcp) =>
            <button
              key={mcp.id}
              onClick={() => onToggle(mcp.id)}
              disabled={!mcp.enabled}
              className={`w-full flex items-center justify-between px-3 py-2 text-xs text-left transition-colors ${mcp.enabled ? 'text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]' : 'text-[hsl(var(--muted-foreground))] opacity-50 cursor-not-allowed'}`}>

                  <span className="flex items-center gap-2 truncate">
                    <span className="truncate">{mcp.name}</span>
                    {typeof mcp.count === 'number' &&
                <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                        {mcp.count}
                      </span>
                }
                  </span>
                  <Circle
                className={`w-2 h-2 shrink-0 ${mcp.active ? 'fill-emerald-500 text-emerald-500' : 'fill-[hsl(var(--muted))] text-[hsl(var(--border))]'}`} />

                </button>
            )}
            </div>
          </div>
        }
      </div>);

  }
);
McpSelector.displayName = 'McpSelector';