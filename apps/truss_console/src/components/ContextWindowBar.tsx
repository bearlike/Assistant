import { SessionUsage } from '../types';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import { formatTokens } from '../utils/time';

/**
 * Visual progress bar for the root agent's context window. Replaces the
 * legacy text-only "X left until compact" pill with a Kilocode-/Codex-style
 * 3-segment bar plus a click-to-expand popover that breaks down billable
 * cost, cache savings, sub-agent usage, and compaction history.
 *
 * Source of truth is `root_last_input_tokens` — the size of the most recent
 * root prompt — because that is what the model actually has in its window
 * right now (matches Claude Code's `currentUsage` and Codex's
 * `last_token_usage`). Peak across calls is shown as a secondary detail in
 * the popover for users who care about historical worst-case pressure.
 */
interface Props {
  usage: SessionUsage | null | undefined;
  /** Compact variant for the navbar — narrower bar, no leading "ctx" label. */
  compact?: boolean;
}

export function ContextWindowBar({ usage, compact = false }: Props) {
  if (!usage || usage.root_max_input_tokens <= 0) return null;

  const used = Math.min(usage.root_last_input_tokens, usage.root_max_input_tokens);
  const window_ = usage.root_max_input_tokens;
  const compactAt = Math.floor(window_ * usage.compact_threshold);
  const reservedForCompact = Math.max(0, window_ - compactAt);

  const usedPct = window_ > 0 ? (used / window_) * 100 : 0;
  const reservedPct = window_ > 0 ? (reservedForCompact / window_) * 100 : 0;
  const availPct = Math.max(0, 100 - usedPct - reservedPct);

  // Tone gates: warn at the auto-compact threshold (typically 80%), error
  // when within 10% of the absolute window. Matches the prior CompactionPill
  // semantics so users see the same color states.
  const ratioRemaining =
    window_ > 0 ? usage.tokens_until_compact / window_ : 1;
  const usedFillCls =
    ratioRemaining <= 0.1
      ? 'bg-[hsl(var(--destructive))]'
      : ratioRemaining <= 0.2
        ? 'bg-[hsl(var(--primary))]'
        : 'bg-[hsl(var(--foreground))]/70';

  // Pre-discount billable input is what the provider charges for ROW input.
  // Cache reads are billed lower (Anthropic 0.1×, OpenAI 0.5×) — surface the
  // raw count so the user can apply their provider's discount mentally; we
  // do not hardcode a multiplier because the model identifier alone doesn't
  // tell us the provider's exact contract terms.
  const cacheSaved = usage.total_cache_read_tokens;
  const cacheCreated = usage.total_cache_creation_tokens;
  const reasoning = usage.total_reasoning_tokens;

  const trigger = (
    <button
      type="button"
      className={`flex items-center ${compact ? 'gap-1.5' : 'gap-2'} font-mono text-[10px] text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors`}
      aria-label="Context window usage"
    >
      <div
        className={`relative h-1.5 ${compact ? 'w-16' : 'w-24'} rounded-full overflow-hidden bg-[hsl(var(--muted))]`}
        title={`${formatTokens(used)} of ${formatTokens(window_)} (${Math.round(usedPct)}%)`}
      >
        <div
          className={`absolute inset-y-0 left-0 ${usedFillCls} transition-all`}
          style={{ width: `${usedPct}%` }}
        />
        {reservedPct > 0 && (
          <div
            className="absolute inset-y-0 bg-[hsl(var(--accent))]"
            style={{ left: `${usedPct}%`, width: `${reservedPct}%` }}
            title={`Reserved for auto-compact buffer (${Math.round(usage.compact_threshold * 100)}% threshold)`}
          />
        )}
        {availPct > 0.5 && (
          <div
            className="absolute inset-y-0 right-0"
            style={{ width: `${availPct}%` }}
          />
        )}
      </div>
      <span>
        {formatTokens(used)}/{formatTokens(window_)}
      </span>
    </button>
  );

  return (
    <Popover>
      <PopoverTrigger asChild>{trigger}</PopoverTrigger>
      <PopoverContent className="w-80 text-xs font-mono p-3" side="bottom" align="end">
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-[hsl(var(--muted-foreground))]">Context window</span>
            <span>
              {formatTokens(used)} / {formatTokens(window_)}{' '}
              <span className="text-[hsl(var(--muted-foreground))]">
                ({Math.round(usedPct)}%)
              </span>
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[hsl(var(--muted-foreground))]">Until auto-compact</span>
            <span>{formatTokens(usage.tokens_until_compact)}</span>
          </div>
          <div className="h-px bg-[hsl(var(--border))]" />
          <div className="flex items-center justify-between">
            <span className="text-[hsl(var(--muted-foreground))]">Peak this session</span>
            <span>
              root {formatTokens(usage.root_peak_input_tokens)}
              {usage.sub_agent_count > 0 && (
                <>
                  {' '}
                  · sub {formatTokens(usage.sub_peak_input_tokens)} ({usage.sub_agent_count})
                </>
              )}
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[hsl(var(--muted-foreground))]">Billable input</span>
            <span>
              {formatTokens(usage.total_input_tokens_billed)}
              <span className="text-[hsl(var(--muted-foreground))]">
                {' '}({usage.root_llm_calls + usage.sub_llm_calls} calls)
              </span>
            </span>
          </div>
          <div className="flex items-center justify-between">
            <span className="text-[hsl(var(--muted-foreground))]">Billable output</span>
            <span>{formatTokens(usage.total_output_tokens)}</span>
          </div>
          {(cacheSaved > 0 || cacheCreated > 0) && (
            <>
              <div className="h-px bg-[hsl(var(--border))]" />
              {cacheSaved > 0 && (
                <div className="flex items-center justify-between">
                  <span className="text-[hsl(var(--muted-foreground))]">Cache reads</span>
                  <span title="Anthropic bills cache reads at 0.1× input · OpenAI at 0.5×">
                    {formatTokens(cacheSaved)} discounted
                  </span>
                </div>
              )}
              {cacheCreated > 0 && (
                <div className="flex items-center justify-between">
                  <span className="text-[hsl(var(--muted-foreground))]">Cache writes</span>
                  <span title="Anthropic bills 5-min cache writes at 1.25× input">
                    {formatTokens(cacheCreated)}
                  </span>
                </div>
              )}
            </>
          )}
          {reasoning > 0 && (
            <div className="flex items-center justify-between">
              <span className="text-[hsl(var(--muted-foreground))]">Reasoning output</span>
              <span title="Hidden thinking tokens from extended-thinking / o1-class models — billed as output">
                {formatTokens(reasoning)}
              </span>
            </div>
          )}
          {usage.compaction_count > 0 && (
            <>
              <div className="h-px bg-[hsl(var(--border))]" />
              <div className="flex items-center justify-between">
                <span className="text-[hsl(var(--muted-foreground))]">Compactions</span>
                <span>
                  {usage.compaction_count} · saved{' '}
                  {formatTokens(usage.compaction_tokens_saved)}
                </span>
              </div>
            </>
          )}
          <p className="text-[hsl(var(--muted-foreground))] text-[10px] pt-1 leading-snug">
            "Used" is the size of the most recent root prompt — what the model has in its window now. Peak shows the historical worst case.
          </p>
        </div>
      </PopoverContent>
    </Popover>
  );
}
