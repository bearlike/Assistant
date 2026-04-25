import { ReactNode } from 'react';
import { GitBranch, Eye, Code2 } from 'lucide-react';
import { LogEventCard } from './LogEventCard';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { AgentIdChip, StatusPill } from './agents';
import { MODEL_TAG_CLASS } from '../utils/agents';
import { cn } from '../utils/cn';

interface SpawnAgentCardProps {
  caller?: string;
  childId: string;
  task: string;
  agentType?: string;
  model?: string;
  allowedTools: string[];
  deniedTools: string[];
  acceptance?: string;
  extras: ReadonlyArray<readonly [string, string]>;
  message?: string;
  durationMs?: number;
  timestamp?: string;
}

const TASK_PREVIEW_LEN = 70;

function previewTask(task: string): string {
  if (task.length <= TASK_PREVIEW_LEN) return task;
  return task.slice(0, TASK_PREVIEW_LEN).trimEnd() + '…';
}

/* ── Tiny co-located primitives — spawn-card-only, not worth extracting ── */

function FieldBlock({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="mb-3">
      <div className="text-[9.5px] tracking-wider uppercase font-mono text-[hsl(var(--muted-foreground))] mb-1">
        {label}
      </div>
      {children}
    </div>
  );
}

function ToolChips({ label, tools, tone }: { label: string; tools: string[]; tone: 'allow' | 'deny' }) {
  const palette = tone === 'deny'
    ? 'text-red-500 bg-red-500/10 border-red-500/30 line-through'
    : 'text-[hsl(var(--foreground))] bg-[hsl(var(--muted))] border-[hsl(var(--border))]';
  return (
    <div className="flex items-start gap-2 flex-wrap">
      <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] pt-0.5 shrink-0">
        {label}
      </span>
      <div className="flex flex-wrap gap-1">
        {tools.map((t, i) => (
          <span key={i} className={cn(
            'text-[10.5px] font-mono px-1.5 py-px rounded border whitespace-nowrap',
            palette,
          )}>
            {t}
          </span>
        ))}
      </div>
    </div>
  );
}

function RawBlock({ label, value }: { label: string; value: unknown }) {
  return (
    <div className="border-b border-[hsl(var(--border))] last:border-b-0">
      <div className="px-3 pt-2 pb-0.5 text-[9.5px] tracking-wider uppercase font-mono text-[hsl(var(--muted-foreground))]">
        {label}
      </div>
      <pre className="m-0 px-3 pt-1 pb-2.5 text-[11.5px] leading-[1.55] font-mono text-[hsl(var(--foreground))] opacity-90 whitespace-pre-wrap break-words max-h-[280px] overflow-y-auto">
        {JSON.stringify(value, null, 2)}
      </pre>
    </div>
  );
}

/* ── Card ────────────────────────────────────────────────────────────── */

export function SpawnAgentCard({
  caller,
  childId,
  task,
  agentType,
  model,
  allowedTools,
  deniedTools,
  acceptance,
  extras,
  message,
  durationMs,
  timestamp,
}: SpawnAgentCardProps) {
  const childLabel = `agent-${(childId || '').slice(0, 8) || '…'}`;

  const header = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="font-mono text-xs font-medium">spawn_agent</span>
      {agentType && (
        <span className="font-mono text-[10px] text-agent-7 px-1.5 py-px rounded bg-agent-7/10 border border-agent-7/30 whitespace-nowrap">
          {agentType}
        </span>
      )}
      {model && (
        <span className={MODEL_TAG_CLASS}>{model}</span>
      )}
    </span>
  );

  const badge = (
    <span className="flex items-center gap-1 shrink-0">
      <StatusPill status="submitted" />
      {durationMs != null && (
        <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] whitespace-nowrap pl-1">
          {durationMs}ms
        </span>
      )}
    </span>
  );

  // ALWAYS-VISIBLE branch line — caller └─▸ child   "task preview"
  const branchLine = (
    <div className="flex items-center gap-0 px-3 py-2 border-t border-dashed border-[hsl(var(--border))] bg-[hsl(var(--surface))]/40 font-mono text-[11.5px]">
      {caller && <AgentIdChip agentId={caller} />}
      <span className="px-1.5 text-[hsl(var(--muted-foreground))] opacity-60 shrink-0 tracking-tighter">
        └─▸
      </span>
      <span className="font-mono text-[10.5px] text-agent-7 px-1.5 py-px rounded bg-agent-7/10 border border-agent-7/30 font-medium whitespace-nowrap shrink-0">
        {childLabel}
      </span>
      <span className="ml-2.5 font-sans text-xs text-[hsl(var(--foreground))] opacity-80 min-w-0 truncate">
        {previewTask(task)}
      </span>
    </div>
  );

  return (
    <LogEventCard
      icon={<GitBranch className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={header}
      badge={badge}
      timestamp={timestamp}
      accent="agent-7"
      belowHeader={branchLine}
    >
      <Tabs
        defaultValue="rendered"
        className="w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <TabsList className="h-7 p-0.5 bg-[hsl(var(--surface))] mb-2">
          <TabsTrigger value="rendered" className="h-6 px-2 text-[11px] gap-1">
            <Eye className="w-3 h-3" /> Rendered
          </TabsTrigger>
          <TabsTrigger value="raw" className="h-6 px-2 text-[11px] gap-1">
            <Code2 className="w-3 h-3" /> Raw
          </TabsTrigger>
        </TabsList>

        <TabsContent value="rendered" className="mt-0">
          <FieldBlock label="task">
            <div className="font-mono text-xs text-[hsl(var(--foreground))] opacity-90 whitespace-pre-wrap leading-[1.55] max-h-[260px] overflow-y-auto">
              {task}
            </div>
          </FieldBlock>

          {acceptance && (
            <FieldBlock label="acceptance_criteria">
              <div className="font-mono text-xs text-[hsl(var(--foreground))] opacity-85 pl-2 border-l-2 border-emerald-500/50 whitespace-pre-wrap">
                {acceptance}
              </div>
            </FieldBlock>
          )}

          {(allowedTools.length > 0 || deniedTools.length > 0) ? (
            <FieldBlock label="scope">
              <div className="flex flex-col gap-1.5">
                {allowedTools.length > 0 && (
                  <ToolChips label="allow" tools={allowedTools} tone="allow" />
                )}
                {deniedTools.length > 0 && (
                  <ToolChips label="deny" tools={deniedTools} tone="deny" />
                )}
              </div>
            </FieldBlock>
          ) : (
            <FieldBlock label="scope">
              <span className="font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
                (all tools)
              </span>
            </FieldBlock>
          )}

          {extras.length > 0 && (
            <FieldBlock label="extra">
              {extras.map(([k, v]) => (
                <div key={k} className="flex gap-2 items-baseline font-mono text-[11px] mb-0.5">
                  <span className="text-[hsl(var(--muted-foreground))] shrink-0">{k}</span>
                  <span className="text-[hsl(var(--foreground))] opacity-85 min-w-0 truncate">
                    {v}
                  </span>
                </div>
              ))}
            </FieldBlock>
          )}

          <div className="mt-3 pt-2.5 border-t border-dashed border-[hsl(var(--border))] flex items-center gap-2 flex-wrap font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
            <span className="text-[9.5px] tracking-wider uppercase">result</span>
            <span>
              <span>agent_id=</span>
              <span className="text-agent-7">{childId || '—'}</span>
            </span>
            <span>
              <span>status=</span>
              <span className="text-[hsl(var(--muted-foreground))]">submitted</span>
            </span>
          </div>
        </TabsContent>

        <TabsContent value="raw" className="mt-0 -mx-3 -mb-3">
          <RawBlock label="input" value={{
            task,
            ...(agentType ? { agent_type: agentType } : {}),
            ...(model ? { model } : {}),
            ...(allowedTools.length ? { allowed_tools: allowedTools } : {}),
            ...(deniedTools.length ? { denied_tools: deniedTools } : {}),
            ...(acceptance ? { acceptance_criteria: acceptance } : {}),
            ...Object.fromEntries(extras),
          }} />
          <RawBlock label="output" value={{
            agent_id: childId,
            status: 'submitted',
            task: task.slice(0, 200),
            message: message ?? '',
          }} />
        </TabsContent>
      </Tabs>
    </LogEventCard>
  );
}
