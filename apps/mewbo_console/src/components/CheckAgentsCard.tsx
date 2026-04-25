import { useMemo } from 'react';
import { Network, Eye, Code2 } from 'lucide-react';
import { LogEventCard } from './LogEventCard';
import { Tabs, TabsList, TabsTrigger, TabsContent } from './ui/tabs';
import { AgentIdChip, StatusDot, StatusPill } from './agents';
import { AgentTreeNode } from '../types';
import { cn } from '../utils/cn';
import {
  STATUS_ORDER,
  STATUS_STYLES,
  StatusKey,
  isTerminal,
  statusKey,
} from '../utils/agentStatus';

interface CheckAgentsCardProps {
  agents: AgentTreeNode[];
  parentId: string;
  rawText: string;
  wait?: boolean;
  durationMs?: number;
  waitedMs?: number;
  timestamp?: string;
}

interface FlatRow {
  agent: AgentTreeNode;
  depth: number;
  isLastChild: boolean;
  ancestorIsLast: boolean[];
}

/** Depth-first walk producing ancestor-isLast arrays for clean SVG connectors. */
function flattenTree(agents: AgentTreeNode[]): FlatRow[] {
  const sorted = [...agents].sort((a, b) => a.depth - b.depth || a.id.localeCompare(b.id));
  const byParent = new Map<string, AgentTreeNode[]>();
  for (const a of sorted) {
    const p = a.parent_id || '__root';
    const list = byParent.get(p);
    if (list) list.push(a); else byParent.set(p, [a]);
  }
  const out: FlatRow[] = [];
  const walk = (parent: string, ancestorIsLast: boolean[]) => {
    const kids = byParent.get(parent) || [];
    kids.forEach((a, i) => {
      const isLast = i === kids.length - 1;
      out.push({ agent: a, depth: a.depth, isLastChild: isLast, ancestorIsLast: [...ancestorIsLast, isLast] });
      walk(a.id, [...ancestorIsLast, isLast]);
    });
  };
  // Roots are agents whose parent_id isn't itself in the visible set.
  const ids = new Set(sorted.map(a => a.id));
  const rootParents = new Set<string>();
  for (const a of sorted) {
    rootParents.add(!a.parent_id || !ids.has(a.parent_id) ? (a.parent_id || '__root') : '');
  }
  rootParents.delete('');
  rootParents.forEach(rp => walk(rp, []));
  return out;
}

function TreeRow({ row }: { row: FlatRow }) {
  const { agent, depth, isLastChild, ancestorIsLast } = row;
  const key = statusKey(agent.status);
  const s = STATUS_STYLES[key];
  const indent = depth * 20 + 4;
  const showResult = !!agent.result?.summary;
  const showProgress = !showResult && !isTerminal(agent.status) && !!agent.progress_note;
  const inlineText = (agent.result?.summary || agent.progress_note || '').slice(0, 120);
  const inlineEllipsis = (agent.result?.summary || agent.progress_note || '').length > 120;

  return (
    <div className="relative py-1.5 px-0.5">
      {depth > 0 && (
        <svg
          aria-hidden
          className="absolute left-0 top-0 h-full pointer-events-none"
          width={depth * 20 + 4}
          preserveAspectRatio="none"
        >
          {Array.from({ length: depth }).map((_, i) => {
            const x = i * 20 + 10;
            const isTail = i === depth - 1;
            const drawVertical = isTail || !ancestorIsLast[i];
            return (
              <g key={i}>
                {drawVertical && !isTail && (
                  <line x1={x} y1={0} x2={x} y2="100%" stroke="hsl(var(--border-strong))" strokeWidth={1} />
                )}
                {isTail && (
                  <>
                    <line x1={x} y1={0} x2={x} y2={isLastChild ? 14 : '100%'} stroke="hsl(var(--border-strong))" strokeWidth={1} />
                    <line x1={x} y1={14} x2={x + 8} y2={14} stroke="hsl(var(--border-strong))" strokeWidth={1} />
                  </>
                )}
              </g>
            );
          })}
        </svg>
      )}

      <div className="flex items-baseline gap-2 min-w-0" style={{ marginLeft: indent }}>
        <StatusDot status={agent.status} pulse={agent.status === 'running'} className="self-center" />
        <AgentIdChip agentId={agent.id} />
        <span className={cn('shrink-0 self-center text-[10.5px] font-medium font-sans uppercase tracking-wider', s.text)}>
          {agent.status}
        </span>
        <span className="flex-1 min-w-0 text-[13px] font-sans text-[hsl(var(--foreground))] truncate">
          &quot;{agent.task}&quot;
        </span>
      </div>

      <div
        className="flex flex-wrap gap-x-2.5 gap-y-0.5 mt-0.5 text-[11.5px] font-mono text-[hsl(var(--muted-foreground))]"
        style={{ marginLeft: indent + 20 }}
      >
        <span className="shrink-0 whitespace-nowrap">
          {agent.steps_completed} {agent.steps_completed === 1 ? 'step' : 'steps'}
        </span>
        {agent.last_tool_id && (
          <span className="shrink-0 whitespace-nowrap">
            · last: <code className="text-[hsl(var(--foreground))] font-[inherit]">{agent.last_tool_id}</code>
          </span>
        )}
        {agent.compaction_count > 0 && (
          <span className="shrink-0 whitespace-nowrap text-amber-500">
            · compacted ×{agent.compaction_count}
          </span>
        )}
      </div>

      {(showResult || showProgress) && (
        <div
          className={cn(
            'mt-1 pl-2 border-l-2 text-[12.5px] font-sans text-[hsl(var(--foreground))]',
            showResult ? s.border.replace('border-', 'border-l-').replace('/30', '/60') : 'border-l-cyan-500/60',
          )}
          style={{ marginLeft: indent + 20 }}
        >
          <span className={cn('mr-1.5 font-mono text-[9.5px] font-semibold tracking-wider', showResult ? s.text : 'text-cyan-600')}>
            {showResult ? `RESULT(${agent.result?.status || agent.status})` : 'PROGRESS'}
          </span>
          <span className="opacity-90 italic">
            {inlineText}{inlineEllipsis && '…'}
          </span>
        </div>
      )}
    </div>
  );
}

export function CheckAgentsCard({
  agents,
  parentId,
  rawText,
  wait,
  durationMs,
  waitedMs,
  timestamp,
}: CheckAgentsCardProps) {
  const counts = useMemo(() => {
    const c: Partial<Record<StatusKey, number>> = {};
    for (const a of agents) {
      const k = statusKey(a.status);
      c[k] = (c[k] || 0) + 1;
    }
    return c;
  }, [agents]);

  const treeRows = useMemo(() => flattenTree(agents), [agents]);

  const header = (
    <span className="flex items-center gap-2 min-w-0">
      <span className="font-mono text-xs font-medium">check_agents</span>
      {wait && (
        <span className="font-mono text-[10px] text-cyan-600 px-1.5 py-px rounded bg-cyan-500/10 border border-cyan-500/30">
          wait=true
        </span>
      )}
    </span>
  );

  const chips = (
    <span className="flex items-center gap-1 shrink-0">
      {STATUS_ORDER.filter(k => counts[k]).map(k => (
        <StatusPill key={k} status={k} count={counts[k] || 0} />
      ))}
      {(durationMs != null || waitedMs != null) && (
        <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))] whitespace-nowrap pl-1">
          {waitedMs != null ? `${(waitedMs / 1000).toFixed(1)}s wait` : `${durationMs}ms`}
        </span>
      )}
    </span>
  );

  return (
    <LogEventCard
      icon={<Network className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={header}
      badge={chips}
      timestamp={timestamp}
      accent="blue"
    >
      <Tabs
        defaultValue="rendered"
        className="w-full"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-2">
          <TabsList className="h-7 p-0.5 bg-[hsl(var(--surface))]">
            <TabsTrigger value="rendered" className="h-6 px-2 text-[11px] gap-1">
              <Eye className="w-3 h-3" /> Rendered
            </TabsTrigger>
            <TabsTrigger value="raw" className="h-6 px-2 text-[11px] gap-1">
              <Code2 className="w-3 h-3" /> Raw
            </TabsTrigger>
          </TabsList>
          <span className="font-mono text-[9.5px] tracking-wider text-[hsl(var(--muted-foreground))]">
            returned to <code className="text-[hsl(var(--primary))] bg-transparent px-0">{parentId.slice(0, 8)}</code>
          </span>
        </div>

        <TabsContent value="rendered" className="mt-0">
          {agents.length === 0 ? (
            <div className="p-2 font-mono text-xs text-[hsl(var(--muted-foreground))]">
              No agents spawned.
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 mb-2 pb-2 border-b border-dashed border-[hsl(var(--border))] font-mono text-[11px] text-[hsl(var(--muted-foreground))]">
                <span className="text-[9.5px] tracking-wider uppercase">Agents</span>
                {STATUS_ORDER.filter(k => counts[k]).map(k => (
                  <span key={k}>
                    {counts[k]} <span className={STATUS_STYLES[k].text}>{k}</span>
                  </span>
                ))}
              </div>
              {treeRows.map(row => (
                <TreeRow key={row.agent.id} row={row} />
              ))}
            </>
          )}
        </TabsContent>

        <TabsContent value="raw" className="mt-0">
          <pre className="p-3 -mx-3 -mb-3 bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] text-[11.5px] leading-[1.55] font-mono whitespace-pre-wrap break-words">
            {rawText}
          </pre>
        </TabsContent>
      </Tabs>
    </LogEventCard>
  );
}
