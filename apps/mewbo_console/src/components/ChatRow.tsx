import { ReactNode } from 'react';
import { formatSessionTime } from '../utils/time';
import { cn } from '../utils/cn';

interface ChatRowProps {
  timestamp?: string;
  handle: ReactNode;
  bodyClassName?: string;
  children: ReactNode;
}

/**
 * Shared chat-line row used by user_steer, agent_message, and root_steer
 * renderers. Lays out: [timestamp] <handle> body.
 */
export function ChatRow({ timestamp, handle, bodyClassName, children }: ChatRowProps) {
  return (
    <div className="flex items-start gap-2 px-1 py-1">
      {timestamp && (
        <span className="text-[10px] text-[hsl(var(--muted-foreground))] whitespace-nowrap shrink-0 font-mono pt-0.5">
          {formatSessionTime(timestamp)}
        </span>
      )}
      {handle}
      <div className={cn(
        'text-xs text-[hsl(var(--foreground))] leading-relaxed opacity-90 min-w-0 overflow-hidden',
        bodyClassName,
      )}>
        {children}
      </div>
    </div>
  );
}

interface HandleProps {
  from: string;
  to?: string;
  fromColor: string;
  toColor?: string;
}

/**
 * Renders `<from>` or `<from → to>` with the mono/semibold styling shared
 * across chat-line renderers. Colors are passed as Tailwind classes (e.g.
 * `text-blue-400`, `text-agent-3`) so callers can pick from palette tokens
 * or agent-color-cycling.
 */
export function Handle({ from, to, fromColor, toColor }: HandleProps) {
  return (
    <span className="text-xs font-mono font-semibold whitespace-nowrap shrink-0 inline-flex items-baseline">
      <span className={fromColor}>&lt;{from}</span>
      {to && (
        <>
          <span className="text-[hsl(var(--muted-foreground))] font-normal mx-1 opacity-60">→</span>
          <span className={toColor || fromColor}>{to}</span>
        </>
      )}
      <span className={fromColor}>&gt;</span>
    </span>
  );
}
