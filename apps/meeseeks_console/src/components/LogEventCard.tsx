import { ReactNode, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { formatSessionTime } from '../utils/time';

const accentColors = {
  emerald: 'border-l-emerald-500',
  red: 'border-l-red-500',
  amber: 'border-l-amber-500',
  blue: 'border-l-blue-500',
  muted: 'border-l-[hsl(var(--muted-foreground))]',
} as const;

export type AccentColor = keyof typeof accentColors;

interface LogEventCardProps {
  icon: ReactNode;
  title: ReactNode;
  badge?: ReactNode;
  timestamp?: string;
  accent: AccentColor;
  depth?: number;
  defaultExpanded?: boolean;
  children?: ReactNode;
}

export function LogEventCard({
  icon,
  title,
  badge,
  timestamp,
  accent,
  depth = 0,
  defaultExpanded = false,
  children,
}: LogEventCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const hasBody = children != null;
  const depthMargin = Math.min(depth, 3) * 24;

  return (
    <div
      style={depthMargin ? { marginLeft: depthMargin } : undefined}
      className={`rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] border-l-[3px] ${accentColors[accent]} ${hasBody ? 'cursor-pointer' : ''} transition-colors hover:bg-[hsl(var(--accent))]/30`}
      onClick={hasBody ? () => setExpanded((p) => !p) : undefined}
    >
      <div className="flex items-center gap-2 px-3 py-2.5">
        <span className="shrink-0 opacity-70">{icon}</span>
        <span className="text-sm font-medium text-[hsl(var(--foreground))] truncate flex-1">
          {title}
        </span>
        {badge}
        {timestamp && (
          <span className="text-[10px] text-[hsl(var(--muted-foreground))] whitespace-nowrap shrink-0">
            {formatSessionTime(timestamp)}
          </span>
        )}
        {hasBody && (
          <ChevronRight
            className={`w-3 h-3 text-[hsl(var(--muted-foreground))] shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
          />
        )}
      </div>
      {expanded && children && (
        <div className="px-3 pb-3 pt-0 border-t border-[hsl(var(--border))]">
          <div className="pt-2">{children}</div>
        </div>
      )}
    </div>
  );
}
