import { useMemo } from 'react';
import { AlertTriangle, X } from 'lucide-react';
import { formatModelName } from '../utils/model';
import { isUnsupportedModel } from '../utils/modelSupport';
import { ModelBrandIcon } from './ModelBrandIcon';
import { Switch } from './ui/switch';

type ModelFallbackChainProps = {
  /** Every model that may join the ladder. Ordering (unsupported last) is
   *  applied here, so a caller hands over its raw list. */
  models: string[];
  /** The ordered ladder. Empty while `enabled` is on is inert, not invalid —
   *  it mirrors the wire's "omitted or empty means no fallback". */
  value: string[];
  onChange: (next: string[]) => void;
  enabled: boolean;
  onEnabledChange: (enabled: boolean) => void;
  /** Container chrome. Defaults to the composer popover's divider row; a
   *  consumer mounting this inside a form field or a menu passes its own. */
  className?: string;
};

/**
 * The opt-in cross-model fallback ladder: a `Switch` plus an ordered
 * add/remove chain. Extracted from `ModelSelector` so every surface that
 * starts a run — the Tasks composer, the wiki configure wizard, wiki project
 * settings, agentic search — offers the SAME control rather than growing its
 * own. The ladder is a flat `string[]`; there is no separate "enabled" wire
 * field, so a caller persists an empty ladder as null/omitted.
 *
 * Ordering IS priority: models are tried top to bottom after the primary
 * fails, and a click appends.
 */
export function ModelFallbackChain({
  models,
  value,
  onChange,
  enabled,
  onEnabledChange,
  className = 'border-t border-[hsl(var(--border))] shrink-0',
}: ModelFallbackChainProps) {
  // Unsupported (whisper/embedding) models sink to the bottom of the list.
  const candidates = useMemo(
    () =>
      [...models]
        .sort((a, b) => (isUnsupportedModel(a) ? 1 : 0) - (isUnsupportedModel(b) ? 1 : 0))
        .filter((m) => !value.includes(m)),
    [models, value],
  );

  const toggle = (model: string) =>
    onChange(
      value.includes(model) ? value.filter((m) => m !== model) : [...value, model],
    );

  return (
    <div className={className}>
      <label className="flex items-center justify-between gap-2 px-3 pt-2 pb-1.5 cursor-pointer">
        <span className="text-xs font-medium text-[hsl(var(--foreground))]">
          Model fallback
        </span>
        <Switch
          checked={enabled}
          onCheckedChange={onEnabledChange}
          aria-label="Enable model fallback"
        />
      </label>
      {enabled && (
        <div className="pb-2">
          <div className="flex items-start gap-1.5 px-3 pb-1.5 text-xs leading-snug text-[hsl(var(--muted-foreground))]">
            <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5 text-amber-500" />
            <span>
              Falling back to another model can change cost, latency, output
              style, and prompt-cache behaviour. Models are tried in the order
              shown.
            </span>
          </div>
          {/* Selected chain — ordered, with remove buttons. */}
          {value.length > 0 && (
            <div className="px-3 py-1 flex flex-col gap-1">
              {value.map((model, i) => (
                <div
                  key={model}
                  className="flex items-center gap-1.5 text-xs text-[hsl(var(--foreground))]"
                >
                  {/* `tabular-nums`, not mono: this is a decorative list
                      ordinal, and the only thing it needs is for the period
                      to land at the same x on every row. `w-4` fixes the
                      box but not the glyph advance, so proportional digits
                      still wobble. Size is inherited from the row — the
                      fewer places that restate type, the fewer places it
                      can drift. */}
                  <span className="tabular-nums text-[hsl(var(--muted-foreground))] w-4 shrink-0">
                    {i + 1}.
                  </span>
                  <ModelBrandIcon modelId={model} size={12} />
                  <span className="truncate flex-1">{formatModelName(model)}</span>
                  <button
                    type="button"
                    onClick={() => toggle(model)}
                    aria-label={`Remove ${formatModelName(model)} from fallback chain`}
                    className="p-0.5 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
            </div>
          )}
          {/* Add-to-chain list — plain rows, appends on click (priority = click order). */}
          {candidates.length > 0 && (
            <div className="max-h-[150px] overflow-y-auto">
              <div className="px-3 pt-1 pb-0.5 text-2xs font-medium uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
                Add to chain
              </div>
              {candidates.map((model) => {
                const unsupported = isUnsupportedModel(model);
                return (
                  <button
                    key={model}
                    type="button"
                    disabled={unsupported}
                    onClick={() => toggle(model)}
                    title={unsupported ? 'Not supported for chat or agents' : undefined}
                    className={`w-full flex items-center gap-1.5 px-3 py-1.5 text-xs text-left hover:bg-[hsl(var(--accent))] transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${unsupported ? 'text-[hsl(var(--muted-foreground))]' : 'text-[hsl(var(--foreground))]'}`}
                  >
                    <ModelBrandIcon modelId={model} size={14} />
                    {unsupported && (
                      <span role="img" aria-label="Not supported for chat" className="text-amber-500">
                        ⚠️
                      </span>
                    )}
                    <span className="truncate">{formatModelName(model)}</span>
                  </button>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
