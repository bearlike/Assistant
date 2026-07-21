import { useMemo } from 'react';
import {
  ChevronDown,
  ChevronUp,
  Cpu,
} from 'lucide-react';
import { formatModelName } from '../utils/model';
import { getProviderIcon } from '../utils/modelIcon';
import { isUnsupportedModel } from '../utils/modelSupport';
import { ModelBrandIcon } from './ModelBrandIcon';
import { ModelFallbackChain } from './ModelFallbackChain';
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover';
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from './ui/command';
import {
  COMMAND_EMPTY_CLS,
  COMMAND_INPUT_CLS,
  COMMAND_ITEM_SINGLE_LINE_CLS,
  COMMAND_ITEM_TWO_LINE_CLS,
  ErrorBanner,
  RefreshIcon,
} from './ConfigMenu';

type ModelSelectorProps = {
  models: string[];
  defaultModel: string | null;
  activeModel: string | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => void;
  onSelectModel: (model: string | null) => void;
  // Cross-model fallback (opt-in). ``fallbackModels`` is an ordered list; the
  // run tries each after the primary fails. Empty + enabled is inert until the
  // user picks at least one, mirroring the backend's "omit/empty = no fallback".
  fallbackEnabled: boolean;
  fallbackModels: string[];
  onToggleFallbackEnabled: (enabled: boolean) => void;
  onFallbackModelsChange: (next: string[]) => void;
  // Popover control (mirrors ConfigMenu so the two footer menus share a shape).
  open: boolean;
  onToggleOpen: () => void;
  direction?: 'up' | 'down';
  /** Locked while a session is running — the trigger shows the current model
   * but selection is disabled (you can't swap models mid-run). */
  disabled?: boolean;
  compact?: boolean;
};

/**
 * Top-level model control for the composer footer. The trigger is a visible
 * pill showing the ACTIVE model name (Devin's "Fast" label idiom); the popover
 * co-locates the primary model picker and the opt-in fallback chain, because
 * both are model concerns and belong together (they used to be buried in two
 * separate ConfigMenu tabs). Brand icons + name reuse `ModelBrandIcon` +
 * `formatModelName` — no bespoke glyphs.
 */
export function ModelSelector({
  models,
  defaultModel,
  activeModel,
  loading,
  error,
  onRefresh,
  onSelectModel,
  fallbackEnabled,
  fallbackModels,
  onToggleFallbackEnabled,
  onFallbackModelsChange,
  open,
  onToggleOpen,
  direction = 'up',
  disabled = false,
  compact = false,
}: ModelSelectorProps) {
  // Unsupported (whisper/embedding) models sink to the bottom of the list.
  const orderedModels = useMemo(
    () =>
      [...models].sort((a, b) => {
        const aUn = isUnsupportedModel(a) ? 1 : 0;
        const bUn = isUnsupportedModel(b) ? 1 : 0;
        return aUn - bUn;
      }),
    [models],
  );

  const effectiveModelId = activeModel ?? defaultModel;
  const triggerLabel = activeModel ? formatModelName(activeModel) : 'Default';
  const hasBrand = !!effectiveModelId && !!getProviderIcon(effectiveModelId);
  // Fallback counts as "active" only when enabled AND at least one model is
  // chosen — an empty enabled chain is inert (matches the submit-time guard).
  const fallbackActive = fallbackEnabled && fallbackModels.length > 0;

  return (
    <Popover
      open={disabled ? false : open}
      onOpenChange={(next) => {
        if (disabled) return;
        if (next !== open) onToggleOpen();
      }}
    >
      <PopoverTrigger asChild>
        <button
          aria-label={disabled ? 'Model (locked while running)' : 'Select model'}
          disabled={disabled}
          title={disabled ? 'Locked while the agent is running' : `Model: ${triggerLabel}`}
          className={`flex items-center gap-1.5 h-7 ${compact ? 'px-1.5' : 'px-2.5'} rounded-lg hover:bg-[hsl(var(--accent))] text-xs font-normal text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors ${open ? 'bg-[hsl(var(--accent))] text-[hsl(var(--foreground))]' : ''} ${activeModel ? 'text-[hsl(var(--foreground))]' : ''} disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-[hsl(var(--muted-foreground))]`}
        >
          {hasBrand && effectiveModelId ? (
            <ModelBrandIcon modelId={effectiveModelId} size={14} />
          ) : (
            <Cpu className={`w-3.5 h-3.5 ${activeModel ? '' : 'opacity-50'}`} />
          )}
          <span className={`truncate ${compact ? 'max-w-[80px]' : 'max-w-[130px]'}`}>{triggerLabel}</span>
          {fallbackActive && (
            <span className="shrink-0 min-w-[16px] h-4 px-1 inline-flex items-center justify-center rounded-full bg-[hsl(var(--primary))]/15 text-2xs font-medium text-[hsl(var(--primary-text))]">
              +{fallbackModels.length}
            </span>
          )}
          {!compact && (open ? (
            <ChevronUp className="w-3 h-3 opacity-50" />
          ) : (
            <ChevronDown className="w-3 h-3 opacity-50" />
          ))}
        </button>
      </PopoverTrigger>
      <PopoverContent
        side={direction === 'up' ? 'top' : 'bottom'}
        align="start"
        className="w-80 max-h-[460px] p-0 flex flex-col overflow-hidden"
      >
        {/* Primary model picker. */}
        <Command className="min-h-0">
          <div className="flex items-center justify-between pr-2">
            <CommandInput placeholder="Filter models..." className={COMMAND_INPUT_CLS} />
            <RefreshIcon onRefresh={onRefresh} label="Refresh models" />
          </div>
          <ErrorBanner error={error} />
          <CommandList className="max-h-[240px]">
            <CommandEmpty className={COMMAND_EMPTY_CLS}>
              {loading ? 'Loading...' : 'No matches.'}
            </CommandEmpty>
            <CommandItem
              value="__default__ Default"
              onSelect={() => { onSelectModel(null); onToggleOpen(); }}
              className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${!activeModel ? 'font-medium' : ''}`}
            >
              <span className="flex items-center gap-1.5">
                {hasBrand && effectiveModelId && !activeModel ? (
                  <ModelBrandIcon modelId={effectiveModelId} size={14} />
                ) : (
                  <Cpu className="w-3.5 h-3.5 opacity-60" />
                )}
                <span>Default</span>
                {defaultModel && (
                  <span className="text-xs text-[hsl(var(--muted-foreground))]">
                    {formatModelName(defaultModel)}
                  </span>
                )}
              </span>
            </CommandItem>
            {orderedModels.map((model) => {
              const unsupported = isUnsupportedModel(model);
              const isActive = activeModel === model;
              return (
                <CommandItem
                  key={model}
                  value={model}
                  onSelect={() => { onSelectModel(model); onToggleOpen(); }}
                  title={unsupported ? 'Not supported for chat or agents' : undefined}
                  className={`${COMMAND_ITEM_TWO_LINE_CLS} ${unsupported ? 'text-[hsl(var(--muted-foreground))]' : ''} ${isActive ? 'font-medium' : ''}`}
                >
                  <span className="flex items-center gap-1.5">
                    <ModelBrandIcon modelId={model} size={14} />
                    {unsupported && (
                      <span role="img" aria-label="Not supported for chat" className="text-amber-500">
                        ⚠️
                      </span>
                    )}
                    <span>{formatModelName(model)}</span>
                  </span>
                  {model.includes('/') && (
                    <span className="text-xs text-[hsl(var(--muted-foreground))] truncate w-full mt-0.5">
                      {model}
                    </span>
                  )}
                </CommandItem>
              );
            })}
          </CommandList>
        </Command>

        {/* Fallback section — opt-in cross-model retry chain, co-located with
            the primary picker because it is a model concern. */}
        <ModelFallbackChain
          models={orderedModels}
          value={fallbackModels}
          onChange={onFallbackModelsChange}
          enabled={fallbackEnabled}
          onEnabledChange={onToggleFallbackEnabled}
        />
      </PopoverContent>
    </Popover>
  );
}
