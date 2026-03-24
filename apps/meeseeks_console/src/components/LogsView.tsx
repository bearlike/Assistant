import { useRef, useState, useEffect } from 'react';
import {
  Bot,
  CheckCircle2,
  XCircle,
  AlertCircle,
  Shield,
  ShieldCheck,
  ShieldX,
  Terminal,
  MessageSquare,
} from 'lucide-react';
import { SummaryBlock } from './SummaryBlock';
import { LogEventCard, AccentColor } from './LogEventCard';
import { EventRecord, LogEntry } from '../types';
import { buildLogs, extractSummaryTesting } from '../utils/logs';
import { formatSessionTime } from '../utils/time';

function Badge({ children, color }: { children: React.ReactNode; color: string }) {
  const colorMap: Record<string, string> = {
    emerald: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600',
    red: 'border-red-500/30 bg-red-500/10 text-red-600',
    amber: 'border-amber-500/30 bg-amber-500/10 text-amber-600',
    blue: 'border-blue-500/30 bg-blue-500/10 text-blue-600',
    muted: 'border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]',
  };
  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded-full border whitespace-nowrap ${colorMap[color] || colorMap.muted}`}>
      {children}
    </span>
  );
}

function ModelTag({ model }: { model?: string }) {
  if (!model) return null;
  const short = model.includes('/') ? (model.split('/').pop() ?? model) : model;
  return (
    <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded whitespace-nowrap">
      {short}
    </span>
  );
}

/** Hash agent ID to one of 8 cycling colors (Claude Code sub-agent palette). */
function agentColorIndex(agentId: string): number {
  let hash = 0;
  for (let i = 0; i < agentId.length; i++) {
    hash = ((hash << 5) - hash + agentId.charCodeAt(i)) | 0;
  }
  return Math.abs(hash) % 8;
}

const AGENT_COLOR_CLASSES = [
  'text-agent-0', 'text-agent-1', 'text-agent-2', 'text-agent-3',
  'text-agent-4', 'text-agent-5', 'text-agent-6', 'text-agent-7',
] as const;

/** Deterministic accent color for tool calls by tool category. */
function toolAccent(toolId: string): AccentColor {
  const id = toolId.toLowerCase();
  // File/edit tools → violet (agent-4)
  if (id.includes('edit') || id.includes('write') || id.includes('file')) return 'agent-4';
  // Shell/exec tools → orange (agent-5)
  if (id.includes('shell') || id.includes('bash') || id.includes('exec') || id.includes('run')) return 'agent-5';
  // Search/read tools → blue (agent-1)
  if (id.includes('search') || id.includes('read') || id.includes('grep') || id.includes('find')) return 'agent-1';
  // Agent/spawn tools → aqua (agent-7)
  if (id.includes('spawn') || id.includes('agent')) return 'agent-7';
  // MCP tools (prefixed with mcp_) → green (agent-2)
  if (id.startsWith('mcp_') || id.startsWith('mcp-')) return 'agent-2';
  // Fallback: hash the tool name to a color
  return `agent-${agentColorIndex(toolId)}` as AccentColor;
}

function renderPermission(log: LogEntry) {
  const decision = (log.decision || 'pending').toLowerCase();
  const accent: AccentColor = decision === 'allow' ? 'emerald' : decision === 'deny' ? 'red' : 'amber';
  const Icon = decision === 'allow' ? ShieldCheck : decision === 'deny' ? ShieldX : Shield;
  const badgeText = decision === 'allow' ? 'Allowed' : decision === 'deny' ? 'Denied' : 'Pending';
  const badgeColor = accent;

  return (
    <LogEventCard
      key={log.id}
      icon={<Icon className={`w-4 h-4 ${decision === 'deny' ? 'text-red-500' : 'text-permission'}`} />}
      title={log.toolId || 'tool'}
      badge={<Badge color={badgeColor}>{badgeText}</Badge>}
      timestamp={log.timestamp}
      accent={accent}
    >
      {log.operation && (
        <div className="flex items-center gap-2 mb-2">
          <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">Operation</span>
          <span className="text-[10px] font-mono text-[hsl(var(--foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">{log.operation}</span>
        </div>
      )}
      {log.toolInput && (
        <pre className="text-xs font-mono text-[hsl(var(--muted-foreground))] whitespace-pre-wrap leading-relaxed max-h-[150px] overflow-hidden">
          {log.toolInput}
        </pre>
      )}
    </LogEventCard>
  );
}

function renderAgent(log: LogEntry) {
  const action = log.agentAction || 'start';
  const status = log.agentStatus || action;
  const isStart = action === 'start';
  const accent: AccentColor =
    status === 'running' || isStart ? 'blue' :
    status === 'completed' ? 'emerald' :
    status === 'failed' ? 'red' :
    status === 'rejected' ? 'red' :
    'muted';

  const displayId = (log.agentId || '').slice(0, 8);
  const colorIdx = agentColorIndex(log.agentId || '');
  const agentTextColor = AGENT_COLOR_CLASSES[colorIdx];
  const stepsLabel = log.stepsCompleted ? `${log.stepsCompleted} steps` : '';
  const badgeText = isStart ? 'Started' :
    status === 'completed' ? 'Completed' :
    status === 'failed' ? 'Failed' :
    status === 'cancelled' ? 'Cancelled' :
    status === 'rejected' ? 'Rejected' :
    'Stopped';

  const badgeWithSteps = stepsLabel && !isStart ? `${badgeText} · ${stepsLabel}` : badgeText;

  return (
    <LogEventCard
      key={log.id}
      icon={<Bot className={`w-4 h-4 ${agentTextColor}`} />}
      title={<span className="flex items-center gap-2"><span className="font-mono">{displayId}</span><ModelTag model={log.model} /></span>}
      badge={<Badge color={accent}>{badgeWithSteps}</Badge>}
      timestamp={log.timestamp}
      accent={accent}
      depth={log.depth}
    >
      <div className="space-y-1">
        {log.model && (
          <div className="flex items-center gap-2">
            <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
              {log.model}
            </span>
            {typeof log.depth === 'number' && log.depth > 0 && (
              <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                depth {log.depth}
              </span>
            )}
          </div>
        )}
        {log.detail && (
          <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">
            {log.detail}
          </p>
        )}
      </div>
    </LogEventCard>
  );
}

function renderAgentResult(log: LogEntry) {
  const status = log.agentResultStatus || 'completed';
  const accent: AccentColor =
    status === 'completed' ? 'emerald' :
    status === 'failed' ? 'red' :
    status === 'cannot_solve' ? 'amber' :
    'amber';

  const stepsLabel = log.stepsUsed !== undefined ? `${log.stepsUsed} steps` : '';
  const badgeText = status === 'completed' ? 'Completed' :
    status === 'failed' ? 'Failed' :
    status === 'cannot_solve' ? 'Cannot solve' :
    'Partial';

  return (
    <LogEventCard
      key={log.id}
      icon={<Bot className="w-4 h-4 text-blue-500" />}
      title="Agent result"
      badge={<Badge color={accent}>{stepsLabel ? `${badgeText} · ${stepsLabel}` : badgeText}</Badge>}
      timestamp={log.timestamp}
      accent={accent}
      defaultExpanded
    >
      <div className="space-y-2">
        {log.summary && (
          <p className="text-xs text-[hsl(var(--foreground))] leading-relaxed">
            {log.summary}
          </p>
        )}
        {log.artifacts && log.artifacts.length > 0 && (
          <div>
            <span className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider">Artifacts</span>
            <div className="flex flex-wrap gap-1 mt-1">
              {log.artifacts.map((a, i) => (
                <span key={i} className="text-[10px] font-mono text-[hsl(var(--foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded">
                  {a}
                </span>
              ))}
            </div>
          </div>
        )}
        {log.warnings && log.warnings.length > 0 && (
          <div>
            <span className="text-[10px] font-medium text-amber-500 uppercase tracking-wider">Warnings</span>
            {log.warnings.map((w, i) => (
              <p key={i} className="text-xs text-amber-500/80 mt-0.5">{w}</p>
            ))}
          </div>
        )}
      </div>
    </LogEventCard>
  );
}

function renderCompletion(log: LogEntry) {
  const reason = (log.doneReason || '').toLowerCase();
  const accent: AccentColor =
    reason === 'completed' ? 'emerald' :
    reason === 'error' ? 'red' :
    reason === 'canceled' || reason === 'cancelled' ? 'muted' :
    'amber';

  const Icon = reason === 'completed' ? CheckCircle2 :
    reason === 'error' ? AlertCircle :
    reason === 'canceled' || reason === 'cancelled' ? XCircle :
    AlertCircle;

  const label = reason === 'completed' ? 'Run completed' :
    reason === 'canceled' || reason === 'cancelled' ? 'Run canceled' :
    reason === 'error' ? 'Run failed' :
    `Run ${reason || 'ended'}`;

  return (
    <LogEventCard
      key={log.id}
      icon={<Icon className={`w-4 h-4 ${accent === 'emerald' ? 'text-emerald-500' : accent === 'red' ? 'text-red-500' : accent === 'amber' ? 'text-amber-500' : 'text-[hsl(var(--muted-foreground))]'}`} />}
      title={label}
      badge={log.doneReason && log.doneReason !== reason ? <Badge color={accent}>{log.doneReason}</Badge> : undefined}
      timestamp={log.timestamp}
      accent={accent}
      defaultExpanded={!!log.error}
    >
      {log.error && (
        <p className="text-xs text-red-500 font-mono">{log.error}</p>
      )}
    </LogEventCard>
  );
}

function renderShell(log: LogEntry) {
  const hasError = !!log.error;
  const toolName = (log.title || 'tool').replace(/\s*\(.*\)$/, ''); // strip "(set)" suffix for matching
  return (
    <LogEventCard
      key={log.id}
      icon={<Terminal className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={<span className="flex items-center gap-2">{log.title || 'tool'}<ModelTag model={log.model} /></span>}
      badge={hasError ? <Badge color="red">Error</Badge> : undefined}
      timestamp={log.timestamp}
      accent={hasError ? 'red' : toolAccent(toolName)}
    >
      {(log.shellInput || log.shellOutput) ? (
        <div className="space-y-0 -mx-3 -mb-2">
          {log.shellInput && (
            <div className="px-3 py-2 bg-[hsl(var(--muted))]/20">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Input</div>
              <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80 max-h-[200px] overflow-hidden">
                {log.shellInput}
              </pre>
            </div>
          )}
          {log.shellInput && log.shellOutput && (
            <div className="border-b border-[hsl(var(--border))]" />
          )}
          {log.shellOutput && (
            <div className="px-3 py-2">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Output</div>
              <pre className={`text-xs font-mono whitespace-pre-wrap leading-relaxed max-h-[300px] overflow-hidden ${hasError ? 'text-red-500/80' : 'text-[hsl(var(--foreground))] opacity-80'}`}>
                {log.shellOutput}
              </pre>
            </div>
          )}
        </div>
      ) : (
        <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80 max-h-[300px] overflow-hidden">
          {log.content}
        </pre>
      )}
    </LogEventCard>
  );
}

function renderReflection(log: LogEntry) {
  return (
    <LogEventCard
      key={log.id}
      icon={<MessageSquare className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title="Reflection"
      timestamp={log.timestamp}
      accent="muted"
      defaultExpanded
    >
      <p className="text-xs text-[hsl(var(--muted-foreground))] leading-relaxed">{log.content}</p>
    </LogEventCard>
  );
}

export function LogsView({ events }: { events: EventRecord[] }) {
  const logs = buildLogs(events);
  const summaryData = extractSummaryTesting(events);
  const hasSummary =
    summaryData.summary.length > 0 || summaryData.testing.length > 0;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);

  const scrollToBottom = () => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    container.scrollTop = container.scrollHeight;
  };

  useEffect(() => {
    if (isAtBottom) {
      scrollToBottom();
    }
  }, [events.length, isAtBottom]);

  const handleScroll = () => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    const threshold = 64;
    const atBottom =
      container.scrollTop + container.clientHeight >=
      container.scrollHeight - threshold;
    setIsAtBottom(atBottom);
  };
  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        className="h-full overflow-y-auto bg-[hsl(var(--background))] p-4 space-y-2"
      >
        {logs.map((log) => {
          if (log.type === 'plan') {
            return (
              <div
                key={log.id}
                className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4"
              >
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <h3 className="text-sm font-semibold text-[hsl(var(--foreground))]">
                      {log.label || 'Plan'}
                    </h3>
                    {log.version && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
                        v{log.version}
                      </span>
                    )}
                    {log.planMode === 'diff' && (
                      <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--accent))] text-[hsl(var(--muted-foreground))]">
                        diff
                      </span>
                    )}
                  </div>
                  {log.timestamp && (
                    <span className="text-[10px] text-[hsl(var(--muted-foreground))]">
                      {formatSessionTime(log.timestamp)}
                    </span>
                  )}
                </div>
                <ol className="space-y-3">
                  {(log.steps || []).map((step, idx) => (
                    <li key={idx} className="flex gap-3">
                      <span className="text-xs font-mono text-[hsl(var(--muted-foreground))] mt-0.5">
                        {idx + 1}.
                      </span>
                      <div>
                        <div className="flex items-center gap-2 text-sm text-[hsl(var(--foreground))] font-medium">
                          {step.title}
                          {step.diffType && (
                            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]">
                              {step.diffType === 'added' ? 'Added' : step.diffType === 'removed' ? 'Removed' : 'Updated'}
                            </span>
                          )}
                        </div>
                        {step.description && (
                          <div className="text-xs text-[hsl(var(--muted-foreground))] mt-1 leading-relaxed">
                            {step.description}
                          </div>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              </div>
            );
          }
          if (log.type === 'permission') return renderPermission(log);
          if (log.type === 'agent') return renderAgent(log);
          if (log.type === 'agent_result') return renderAgentResult(log);
          if (log.type === 'completion') return renderCompletion(log);
          if (log.type === 'shell') return renderShell(log);
          if (log.type === 'system') return renderReflection(log);

          // Fallback for unknown types
          return (
            <div
              key={log.id}
              className="text-sm text-[hsl(var(--muted-foreground))] pl-1"
            >
              {log.content}
            </div>
          );
        })}

        {hasSummary && (
          <div className="pt-8 border-t border-[hsl(var(--border))] mt-8">
            <h3 className="text-sm font-medium text-[hsl(var(--foreground))] mb-4">
              Preparing pull request
            </h3>
            <SummaryBlock
              summary={summaryData.summary}
              testing={summaryData.testing}
            />
          </div>
        )}
      </div>
      {!isAtBottom && (
        <button
          type="button"
          onClick={() => {
            scrollToBottom();
            setIsAtBottom(true);
          }}
          aria-label="Jump to latest logs"
          className="absolute bottom-4 right-4 h-10 w-10 rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-[hsl(var(--foreground))] shadow-md transition hover:-translate-y-0.5"
        >
          <svg
            viewBox="0 0 24 24"
            aria-hidden="true"
            className="mx-auto h-5 w-5"
          >
            <path
              fill="currentColor"
              d="M12 16.5 5 9.5l1.4-1.4L12 13.7l5.6-5.6L19 9.5z"
            />
          </svg>
        </button>
      )}
    </div>
  );
}
