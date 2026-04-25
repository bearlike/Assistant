import { useElapsed } from '../hooks/useElapsed';
import { formatTokens } from '../utils/time';
import { cn } from '../utils/cn';
import type { RunStatus } from './InputBar';

interface RunTelemetryProps {
  data?: RunStatus;
  variant: 'compact' | 'full';
  className?: string;
}

/**
 * Single source of truth for "what is the run doing right now": phase,
 * agent count, token fill, elapsed wall-clock. Renders in two places —
 * the composer's running-state strip (compact) and the workspace
 * FlowerSpinner (full) — same data, two windows on one truth (P8, E3).
 *
 * Compact: single horizontal line — pulse dot + phase + meta on the right.
 * Full:    two-line block — phase up top, agent cluster + tokens + elapsed
 *          below at a slightly larger type ramp for the spinner context.
 */
export function RunTelemetry({ data, variant, className }: RunTelemetryProps) {
  const elapsed = useElapsed(data?.lastUserTs, true);
  const phase = data?.phase ?? 'Running';
  const agents = data?.agents ?? 0;
  const tokens = data?.tokens ?? 0;

  if (variant === 'compact') {
    return (
      <div
        className={cn(
          'flex items-center gap-2 text-[11px] text-[hsl(var(--muted-foreground))]',
          className,
        )}
      >
        <span
          className="session-cmp-pulse inline-block h-1.5 w-1.5 rounded-full bg-[hsl(var(--permission))] shrink-0"
          aria-hidden
        />
        <span
          className="font-medium text-[hsl(var(--foreground))]/85 truncate"
          title={phase}
        >
          {phase}
        </span>
        <span className="ml-auto inline-flex items-center gap-1.5 font-mono text-[10.5px] tabular-nums shrink-0">
          {agents > 0 && (
            <>
              <span>{agents} agent{agents !== 1 ? 's' : ''}</span>
              {(tokens > 0 || elapsed) && <span className="opacity-40">·</span>}
            </>
          )}
          {tokens > 0 && (
            <>
              <span>{formatTokens(tokens)} tokens</span>
              {elapsed && <span className="opacity-40">·</span>}
            </>
          )}
          {elapsed && <span>{elapsed}</span>}
        </span>
      </div>
    );
  }

  // full
  return (
    <div className={cn('flex flex-col gap-1 min-w-0', className)}>
      <span
        className="text-xs font-medium text-[hsl(var(--foreground))] truncate"
        title={phase}
      >
        {phase}
      </span>
      <div className="flex items-center gap-2 text-[11px] font-mono tabular-nums text-[hsl(var(--muted-foreground))]">
        {agents > 0 && (
          <>
            <span className="agents-cluster">
              <span className="agents-stack" aria-hidden>
                {Array.from({ length: Math.min(agents, 4) }).map((_, i) => (
                  <span key={i} className="agent-chip" />
                ))}
              </span>
              <span>{agents} agent{agents !== 1 ? 's' : ''}</span>
            </span>
            {(tokens > 0 || elapsed) && <span className="opacity-40">·</span>}
          </>
        )}
        {tokens > 0 && (
          <>
            <span>{formatTokens(tokens)} tokens</span>
            {elapsed && <span className="opacity-40">·</span>}
          </>
        )}
        {elapsed && <span>{elapsed}</span>}
      </div>
    </div>
  );
}
