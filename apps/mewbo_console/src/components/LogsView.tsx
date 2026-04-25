import {
  Bot,
  CheckCircle2,
  XCircle,
  AlertCircle,
  ArrowRight,
  Shield,
  ShieldCheck,
  ShieldX,
  Terminal,
  MessageSquare,
  RotateCcw,
  Play,
  Layers,
} from 'lucide-react';
import { SummaryBlock } from './SummaryBlock';
import { MarkdownContent } from './MessageBubble';
import { Button } from './ui/button';
import { LogEventCard, AccentColor } from './LogEventCard';
import { ModelLabel } from './ModelLabel';
import { TerminalCard } from './TerminalCard';
import { DiffCard } from './DiffCard';
import { FileReadCard } from './FileReadCard';
import { ScrollToBottom } from './ScrollToBottom';
import { RunTelemetry } from './RunTelemetry';
import type { RunStatus } from './InputBar';
import { ChatRow, Handle } from './ChatRow';
import { CheckAgentsCard } from './CheckAgentsCard';
import { SpawnAgentCard } from './SpawnAgentCard';
import { useAutoScroll } from '../hooks/useAutoScroll';
import { EventRecord, LogEntry } from '../types';
import { formatTokens, formatSessionTime } from '../utils/time';
import { buildLogs, extractSummaryTesting } from '../utils/logs';
import { prettyJsonIfValid } from '../utils/json';
import { AgentIdChip, Badge } from './agents';
import {
  AGENT_COLOR_CLASSES,
  MODEL_TAG_CLASS,
  agentColorIndex,
} from '../utils/agents';

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
  const inTok = log.inputTokens || 0;
  const outTok = log.outputTokens || 0;
  const tokensLabel = !isStart && (inTok + outTok) > 0
    ? `${formatTokens(inTok)} in / ${formatTokens(outTok)} out`
    : '';
  const badgeText = isStart ? 'Started' :
    status === 'completed' ? 'Completed' :
    status === 'failed' ? 'Failed' :
    status === 'cancelled' ? 'Cancelled' :
    status === 'rejected' ? 'Rejected' :
    'Stopped';

  const badgeParts = [badgeText, stepsLabel, tokensLabel].filter(Boolean);
  const badgeWithSteps = !isStart && badgeParts.length > 1 ? badgeParts.join(' · ') : badgeText;

  return (
    <LogEventCard
      key={log.id}
      icon={<Bot className={`w-4 h-4 ${agentTextColor}`} />}
      title={<span className="flex items-center gap-2"><span className="font-mono">{displayId}</span><ModelLabel modelId={log.model} className={MODEL_TAG_CLASS} /></span>}
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

function renderCompletion(
  log: LogEntry,
  onRetry?: () => void,
  onContinue?: () => void,
) {
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
    reason === 'max_steps_reached' ? 'Task interrupted — step limit reached' :
    `Run ${reason || 'ended'}`;

  const isRecoverable = reason === 'error' || reason === 'max_steps_reached';
  const showRecovery = isRecoverable && !!onRetry && !!onContinue;

  return (
    <LogEventCard
      key={log.id}
      icon={<Icon className={`w-4 h-4 ${accent === 'emerald' ? 'text-emerald-500' : accent === 'red' ? 'text-red-500' : accent === 'amber' ? 'text-amber-500' : 'text-[hsl(var(--muted-foreground))]'}`} />}
      title={label}
      badge={log.doneReason && log.doneReason !== reason ? <Badge color={accent}>{log.doneReason}</Badge> : undefined}
      timestamp={log.timestamp}
      accent={accent}
      defaultExpanded={!!log.error || showRecovery}
    >
      {log.error && (
        <p className="text-xs text-red-500 font-mono">{log.error}</p>
      )}
      {showRecovery && (
        <div className="mt-2 flex gap-2">
          <Button
            variant="neutral"
            size="sm"
            tone="info"
            leadingIcon={<RotateCcw className="w-3 h-3" />}
            onClick={onRetry}
            title="Re-run the last user query from where it was submitted"
          >
            Retry
          </Button>
          <Button
            variant="neutral"
            size="sm"
            tone="warn"
            leadingIcon={<Play className="w-3 h-3" />}
            onClick={onContinue}
            title="Resume the session and let the agent recover from where it left off"
          >
            Continue
          </Button>
        </div>
      )}
    </LogEventCard>
  );
}

function renderShell(log: LogEntry) {
  // Structured shell result → TerminalCard
  if (log.shellCommand) {
    return (
      <TerminalCard
        key={log.id}
        command={log.shellCommand}
        cwd={log.shellCwd}
        exitCode={log.shellExitCode}
        stdout={log.shellStdout}
        stderr={log.shellStderr}
        durationMs={log.shellDurationMs}
        model={log.model}
        agentId={log.agentId}
      />
    );
  }

  // Fallback: non-shell tools or old events → existing LogEventCard
  const hasError = !!log.error;
  const toolName = (log.title || 'tool').replace(/\s*\(.*\)$/, '');
  return (
    <LogEventCard
      key={log.id}
      icon={<Terminal className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={<span className="flex items-center gap-2">{log.title || 'tool'}<ModelLabel modelId={log.model} className={MODEL_TAG_CLASS} /><AgentIdChip agentId={log.agentId} /></span>}
      badge={hasError ? <Badge color="red">Error</Badge> : undefined}
      timestamp={log.timestamp}
      accent={hasError ? 'red' : toolAccent(toolName)}
    >
      {(log.shellInput || log.shellOutput) ? (
        <div className="space-y-0 -mx-3 -mb-2">
          {log.shellInput && (
            <div className="px-3 py-2 bg-[hsl(var(--muted))]/20">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Input</div>
              <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80 max-h-[300px] overflow-y-auto">
                {prettyJsonIfValid(log.shellInput)}
              </pre>
            </div>
          )}
          {log.shellInput && log.shellOutput && (
            <div className="border-b border-[hsl(var(--border))]" />
          )}
          {log.shellOutput && (
            <div className="px-3 py-2">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Output</div>
              <pre className={`text-xs font-mono whitespace-pre-wrap leading-relaxed max-h-[500px] overflow-y-auto ${hasError ? 'text-red-500/80' : 'text-[hsl(var(--foreground))] opacity-80'}`}>
                {prettyJsonIfValid(log.shellOutput)}
              </pre>
            </div>
          )}
        </div>
      ) : (
        <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80 max-h-[500px] overflow-y-auto">
          {prettyJsonIfValid(log.content)}
        </pre>
      )}
    </LogEventCard>
  );
}

function renderDiff(log: LogEntry) {
  return (
    <DiffCard
      key={log.id}
      title={log.diffTitle}
      diffText={log.diffText || ''}
      success={log.diffSuccess}
    />
  );
}

function renderFileRead(log: LogEntry) {
  return (
    <FileReadCard
      key={log.id}
      path={log.fileReadPath ?? '(unknown)'}
      text={log.fileReadText ?? ''}
      totalLines={log.fileReadTotalLines}
      timestamp={log.timestamp}
      model={log.model}
      agentId={log.agentId}
    />
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

function renderCompaction(log: LogEntry) {
  const saved = log.tokensSaved ?? 0;
  const hasSavings = saved > 0;
  const pct = hasSavings && log.tokensBefore
    ? Math.round((saved / log.tokensBefore) * 100)
    : null;
  const hasSummary = log.compactSummary != null && log.compactSummary.length > 0;

  // Build badge: show token savings when meaningful, otherwise event count
  const badgeText = hasSavings
    ? `${saved.toLocaleString()} tokens freed`
    : log.eventsSummarized
      ? `${log.eventsSummarized} events`
      : log.compactMode || 'auto';

  // Build metrics parts for the detail line
  const metricParts: string[] = [];
  if (hasSavings && log.tokensBefore != null && log.tokensAfter != null) {
    metricParts.push(`${log.tokensBefore.toLocaleString()} → ${log.tokensAfter.toLocaleString()} tokens${pct != null ? ` (${pct}% reduction)` : ''}`);
  } else if (log.tokensBefore != null) {
    metricParts.push(`${log.tokensBefore.toLocaleString()} tokens in context`);
  }
  if (log.eventsSummarized != null) {
    metricParts.push(`${log.eventsSummarized} events summarized`);
  }

  return (
    <LogEventCard
      key={log.id}
      icon={<Layers className="w-4 h-4 text-blue-400" />}
      title={<span className="flex items-center gap-2">Context compacted<ModelLabel modelId={log.model} className={MODEL_TAG_CLASS} /><AgentIdChip agentId={log.agentId} /></span>}
      badge={<Badge color={hasSavings ? 'blue' : 'muted'}>{badgeText}</Badge>}
      timestamp={log.timestamp}
      accent="blue"
    >
      <div className="space-y-2">
        {metricParts.length > 0 && (
          <p className="text-xs text-[hsl(var(--muted-foreground))]">
            {metricParts.join(' · ')}
          </p>
        )}
        {hasSummary ? (
          <div className="text-xs text-[hsl(var(--foreground))] leading-relaxed opacity-90 [&_h1]:text-sm [&_h1]:font-semibold [&_h1]:mt-2 [&_h2]:text-xs [&_h2]:font-semibold [&_h2]:mt-2 [&_h3]:text-xs [&_h3]:font-semibold [&_h3]:mt-1 [&_ul]:ml-3 [&_ul]:list-disc [&_li]:mb-0.5 [&_p]:mb-1 [&_p:last-child]:mb-0">
            <MarkdownContent content={log.compactSummary ?? ''} />
          </div>
        ) : (
          <p className="text-xs text-[hsl(var(--muted-foreground))] italic">
            Summary unavailable — structured compaction failed, using lightweight fallback.
          </p>
        )}
      </div>
    </LogEventCard>
  );
}

function renderAgentMessage(log: LogEntry) {
  const colorIdx = agentColorIndex(log.agentId || 'root');
  const agentColor = AGENT_COLOR_CLASSES[colorIdx];
  const agentName = log.detail || 'root';
  return (
    <ChatRow
      key={log.id}
      timestamp={log.timestamp}
      handle={<Handle from={agentName} fromColor={agentColor} />}
      bodyClassName="opacity-80 [&_pre]:text-[11px] [&_p]:mb-1 [&_p:last-child]:mb-0"
    >
      <MarkdownContent content={log.content} />
    </ChatRow>
  );
}

function renderUserSteer(log: LogEntry) {
  return (
    <ChatRow
      key={log.id}
      timestamp={log.timestamp}
      handle={<Handle from="user" fromColor="text-blue-400" />}
    >
      {log.content}
    </ChatRow>
  );
}

function renderCheckAgents(log: LogEntry) {
  return (
    <CheckAgentsCard
      key={log.id}
      agents={log.agents || []}
      parentId={log.parentId || ''}
      rawText={log.rawText || ''}
      wait={log.wait}
      durationMs={log.durationMs}
      waitedMs={log.waitedMs}
      timestamp={log.timestamp}
    />
  );
}

function renderSpawnSubmit(log: LogEntry) {
  return (
    <SpawnAgentCard
      key={log.id}
      caller={log.spawnCaller}
      childId={log.spawnChildId || ''}
      task={log.spawnTask || ''}
      agentType={log.spawnAgentType}
      model={log.spawnModel}
      allowedTools={log.spawnAllowedTools || []}
      deniedTools={log.spawnDeniedTools || []}
      acceptance={log.spawnAcceptance}
      extras={log.spawnExtras || []}
      message={log.spawnMessage}
      durationMs={log.spawnDurationMs}
      timestamp={log.timestamp}
    />
  );
}

function renderRootSteer(log: LogEntry) {
  const fullId = log.steerTargetFullId || log.steerTargetPrefix || '';
  const targetLabel = fullId
    ? `agent-${fullId.slice(0, 6)}`
    : (log.steerTargetPrefix || 'agent');
  const targetColor = fullId
    ? AGENT_COLOR_CLASSES[agentColorIndex(fullId)]
    : 'text-[hsl(var(--muted-foreground))]';
  const isCancel = log.steerAction === 'cancel';

  return (
    <div key={log.id}>
      <ChatRow
        timestamp={log.timestamp}
        handle={
          <Handle
            from="root"
            to={targetLabel}
            fromColor="text-[hsl(var(--primary))]"
            toColor={targetColor}
          />
        }
        bodyClassName={isCancel ? 'italic opacity-65 font-mono text-[11.5px]' : undefined}
      >
        {isCancel ? '(cancelled)' : (log.steerMessage || log.content)}
      </ChatRow>
      {log.steerIsError && log.steerResult && (
        <ChatRow
          handle={<Handle from="system" fromColor="text-red-400" />}
          bodyClassName="text-red-400 font-mono text-[11.5px]"
        >
          {log.steerResult}
        </ChatRow>
      )}
    </div>
  );
}

/**
 * Branded conic-ring loader local to the workspace spinner. Single use per
 * E4 — no shared component for one consumer. The conic-gradient ring spins
 * via `.flower-mark`; the centered logo breathes via `.flower-mark-logo`.
 */
function FlowerMark() {
  return (
    <span className="flower-mark" aria-hidden>
      <img className="flower-mark-logo" src="/logo-transparent.svg" alt="" />
    </span>
  );
}

export function LogsView({
  events,
  onRetry,
  onContinue,
  isRunning,
  runStatus,
  isViewingLive,
  onShowLiveTrace,
}: {
  events: EventRecord[];
  onRetry?: () => void;
  onContinue?: () => void;
  isRunning?: boolean;
  runStatus?: RunStatus;
  isViewingLive?: boolean;
  onShowLiveTrace?: () => void;
}) {
  const logs = buildLogs(events);
  const summaryData = extractSummaryTesting(events);
  const hasSummary =
    summaryData.summary.length > 0 || summaryData.testing.length > 0;
  // "No logs" empty state — only when this view is frozen (idle, or viewing
  // a past trace while another runs). Suppressed while the events shown are
  // the live, still-streaming turn so we don't flash "empty" before the
  // first tool call lands. The live-spinner / jump-to-live footer still
  // renders independently below.
  const showEmpty = logs.length === 0 && !hasSummary && !(isRunning && isViewingLive);
  const { scrollRef, isAtBottom, scrollToBottom, onScroll } = useAutoScroll(events.length);
  return (
    <div className="relative h-full">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-y-auto p-5 space-y-4"
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
          if (log.type === 'completion') return renderCompletion(log, onRetry, onContinue);
          if (log.type === 'diff') return renderDiff(log);
          if (log.type === 'file_read') return renderFileRead(log);
          if (log.type === 'shell') return renderShell(log);
          if (log.type === 'agent_message') return renderAgentMessage(log);
          if (log.type === 'user_steer') return renderUserSteer(log);
          if (log.type === 'check_agents') return renderCheckAgents(log);
          if (log.type === 'root_steer') return renderRootSteer(log);
          if (log.type === 'spawn_submit') return renderSpawnSubmit(log);
          if (log.type === 'compact') return renderCompaction(log);
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

        {showEmpty && (
          <div className="flex items-center justify-center py-16 text-xs text-[hsl(var(--muted-foreground))]">
            No logs to show here
          </div>
        )}
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
        {isRunning && isViewingLive && (
          <div className="spinner-sticky" role="status" aria-live="polite">
            <div className="spinner-row">
              <FlowerMark />
              <RunTelemetry data={runStatus} variant="full" className="flex-1" />
            </div>
          </div>
        )}
        {isRunning && !isViewingLive && onShowLiveTrace && (
          <div className="spinner-sticky" role="status" aria-live="polite">
            <button
              type="button"
              onClick={onShowLiveTrace}
              className="spinner-row w-full text-left text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--card))]/85 transition-colors"
              title="Jump to the live trace"
            >
              <span className="pending-dot shrink-0" aria-hidden />
              <span className="flex-1 truncate">
                Live trace running elsewhere
              </span>
              <ArrowRight className="w-3.5 h-3.5 shrink-0 opacity-70" aria-hidden />
            </button>
          </div>
        )}
      </div>
      {!isAtBottom && (
        <ScrollToBottom onClick={scrollToBottom} label="Jump to latest logs" />
      )}
    </div>
  );
}
