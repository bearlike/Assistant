import { forwardRef } from 'react';
import { ChevronDown, ChevronUp, Circle, RefreshCw } from 'lucide-react';
import { Popover } from './Popover';

export type McpStatus = 'active' | 'disabled' | 'error';

export type McpOption = {
  id: string;
  name: string;
  active: boolean;
  enabled: boolean;
  count?: number;
  status: McpStatus;
  scope?: string;
};

type McpSelectorProps = {
  options: McpOption[];
  isOpen: boolean;
  loading?: boolean;
  error?: string | null;
  direction?: 'up' | 'down';
  onRefresh?: () => void;
  onToggleOpen: () => void;
  onToggle: (id: string) => void;
};

const DOT_CLASSES: Record<McpStatus, string> = {
  active: 'fill-emerald-500 text-emerald-500',
  disabled: 'fill-amber-500 text-amber-500',
  error: 'fill-red-500 text-red-500',
};

function StatusDot({ status, active }: { status: McpStatus; active: boolean }) {
  if (status === 'active' && !active) {
    return <Circle className="w-2 h-2 shrink-0 fill-[hsl(var(--muted))] text-[hsl(var(--border))]" />;
  }
  return <Circle className={`w-2 h-2 shrink-0 ${DOT_CLASSES[status]}`} />;
}

function ScopeHeader({ label }: { label: string }) {
  return (
    <div className="px-3 pt-2 pb-1">
      <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
        {label}
      </span>
    </div>
  );
}

export const McpSelector = forwardRef<HTMLDivElement, McpSelectorProps>(
  (
    { options, isOpen, loading = false, error, direction = 'up', onRefresh, onToggleOpen, onToggle },
    ref
  ) => {
    const activeCount = options.filter((mcp) => mcp.active).length;

    const globalOptions = options.filter((o) => o.scope !== 'project');
    const projectOptions = options.filter((o) => o.scope === 'project');

    return (
      <div className="relative" ref={ref}>
        <button
          onClick={onToggleOpen}
          aria-label="Select MCP tools"
          className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${isOpen ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''}`}
        >
          <span>{loading ? 'Loading...' : `${activeCount} MCPs`}</span>
          {isOpen ? (
            <ChevronUp className="w-3 h-3 opacity-50" />
          ) : (
            <ChevronDown className="w-3 h-3 opacity-50" />
          )}
        </button>

        {isOpen && (
          <Popover direction={direction} width="w-64" maxHeight="max-h-[320px]">
            <div className="flex items-center justify-between px-3 pt-2 pb-1">
              <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">
                MCP Tools
              </span>
              {onRefresh && (
                <button
                  onClick={onRefresh}
                  aria-label="Refresh MCP tools"
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
            {options.length === 0 && !loading && (
              <div className="px-4 py-3 text-xs text-[hsl(var(--muted-foreground))] text-center">
                No MCP servers available.
              </div>
            )}
            {globalOptions.length > 0 && (
              <>
                <ScopeHeader label="Global" />
                <McpOptionList options={globalOptions} onToggle={onToggle} />
              </>
            )}
            {projectOptions.length > 0 && (
              <>
                {globalOptions.length > 0 && (
                  <div className="h-px bg-[hsl(var(--border))] mx-2 my-1" />
                )}
                <ScopeHeader label="Project" />
                <McpOptionList options={projectOptions} onToggle={onToggle} />
              </>
            )}
          </Popover>
        )}
      </div>
    );
  }
);
McpSelector.displayName = 'McpSelector';

function McpOptionList({ options, onToggle }: { options: McpOption[]; onToggle: (id: string) => void }) {
  return (
    <div className="py-0.5">
      {options.map((mcp) => (
        <button
          key={mcp.id}
          onClick={() => onToggle(mcp.id)}
          disabled={mcp.status === 'error'}
          className={`w-full flex items-center justify-between px-3 py-1.5 text-xs text-left transition-colors ${
            mcp.status === 'error'
              ? 'text-[hsl(var(--muted-foreground))] opacity-60 cursor-not-allowed'
              : mcp.enabled
                ? 'text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]'
                : 'text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))]'
          }`}
        >
          <span className="flex items-center gap-2 truncate">
            <span className="truncate">{mcp.name}</span>
            {typeof mcp.count === 'number' && (
              <span className="text-[10px] text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                {mcp.count}
              </span>
            )}
          </span>
          <StatusDot status={mcp.status} active={mcp.active} />
        </button>
      ))}
    </div>
  );
}
