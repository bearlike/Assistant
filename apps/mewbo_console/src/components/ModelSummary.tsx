import { ModelLabel } from './ModelLabel';
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from './ui/popover';

type Props = {
  /** Distinct model IDs to display, in first-seen order. */
  models?: string[];
  /** The headline / current model (shown in the trigger for multi-model). */
  current?: string | null;
  className?: string;
};

/**
 * Renders the model(s) used in a session in the navbar subtitle.
 *
 * - 0 models → returns null (no-op; preserves "no model metadata → render nothing").
 * - 1 model → renders ModelLabel inline, identical to today's behavior.
 * - N models → shadcn Popover trigger shows current model + muted "+{n-1}" badge;
 *   popover body lists all models with "current" annotation on the headline one.
 */
export function ModelSummary({ models = [], current, className = '' }: Props) {
  // Order-preserving de-dupe.
  const unique = [...new Set(models.filter(Boolean))];

  if (unique.length === 0) return null;

  const labelClass = `text-[10px] font-mono text-[hsl(var(--muted-foreground))] truncate ${className}`.trim();

  if (unique.length === 1) {
    return <ModelLabel modelId={unique[0]} className={labelClass} />;
  }

  // Multiple models: popover.
  const headline = current ?? unique[0];
  const extra = unique.length - 1;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          className="inline-flex items-center gap-1 max-w-[160px] focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[hsl(var(--ring))] rounded-sm"
          aria-label={`${unique.length} models used — click to see all`}
          title={`${unique.length} models used — click to see all`}
        >
          <ModelLabel modelId={headline} className={labelClass} />
          <span className="shrink-0 text-[10px] font-mono text-[hsl(var(--muted-foreground))] opacity-70">
            ·&nbsp;+{extra}
          </span>
        </button>
      </PopoverTrigger>
      <PopoverContent
        side="bottom"
        align="start"
        className="w-auto min-w-[180px] max-w-[280px] p-2"
      >
        <p className="text-[10px] font-semibold uppercase tracking-wide text-[hsl(var(--muted-foreground))] mb-1.5 px-1">
          Models used
        </p>
        <ul className="flex flex-col gap-0.5">
          {unique.map((id) => {
            const isCurrent = id === headline;
            return (
              <li
                key={id}
                className="flex items-center justify-between gap-2 px-1 py-0.5"
              >
                <ModelLabel
                  modelId={id}
                  className="text-[11px] font-mono text-[hsl(var(--foreground))] truncate"
                />
                {isCurrent && (
                  <span className="shrink-0 text-[9px] font-medium text-[hsl(var(--muted-foreground))] border border-[hsl(var(--border))] rounded-sm px-1 py-px leading-none">
                    current
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
