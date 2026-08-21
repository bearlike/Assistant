import { useMemo, useState } from 'react';
import { AlertTriangle, Cpu } from 'lucide-react';
import { formatModelName } from '../utils/model';
import { getProviderIcon } from '../utils/modelIcon';
import { isUnsupportedModel } from '../utils/modelSupport';
import { ModelBrandIcon } from './ModelBrandIcon';
import { FallbackChainRow } from './ModelFallbackChain';
import { Switch } from './ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
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

type ModelPickerTabsProps = {
  /** Every model that may be picked or join the ladder. Ordering
   *  (unsupported last) is applied here, so a caller hands over its raw
   *  list. */
  models: string[];
  /** The selected primary model. `null` means the Default row. */
  value: string | null;
  /** Emits the picked model, or `null` for the Default row. The CALLER owns
   *  any close-on-pick behaviour — wrap this to dismiss your container. */
  onSelect: (model: string | null) => void;
  /** When set, a Default row tops the Model tab and emits `null`. */
  defaultLabel?: string;
  /** The model the Default row resolves to — shown as the brand icon and a
   *  muted hint, so "Default" never reads as a mystery. */
  defaultModel?: string | null;
  loading?: boolean;
  error?: string | null;
  onRefresh?: () => void;
  // Cross-model fallback (opt-in). `fallbackModels` is an ordered ladder; a
  // run tries each after the primary fails. Enabled + empty is inert,
  // mirroring the backend's "omit/empty = no fallback".
  fallbackEnabled: boolean;
  fallbackModels: string[];
  onFallbackEnabledChange: (enabled: boolean) => void;
  onFallbackModelsChange: (next: string[]) => void;
};

/**
 * The ONE model-picker body for every surface that pairs a primary model
 * with a fallback ladder — the Tasks composer popover, the wiki configure
 * wizard, wiki project settings, the agentic-search scope menu. A tab strip
 * (Model | Fallback) over a single `Command`, so one full-bleed search row +
 * refresh button below the tabs filters whichever list is active. Stacking
 * both lists in one scrolling pane stranded the filter input's border short
 * of the panel edge and turned an armed chain into a linear mess; tabs keep
 * each concern in its own place.
 *
 * The Fallback trigger carries a state dot — green when a chain is armed,
 * red while inert — with the state restated in sr-only copy (colour is
 * never the only signal). The armed chain renders as plain rows PINNED above
 * the candidates, so a filter can never re-sort their priority ordinals;
 * picking a candidate appends it to the chain (priority = click order).
 *
 * Container-agnostic: mount it in a `PopoverContent` or a
 * `DropdownMenuSubContent`. Mounted children reset on unmount, so the tab
 * and the filter start fresh every open with no effect needed.
 */
export function ModelPickerTabs({
  models,
  value,
  onSelect,
  defaultLabel,
  defaultModel,
  loading = false,
  error = null,
  onRefresh,
  fallbackEnabled,
  fallbackModels,
  onFallbackEnabledChange,
  onFallbackModelsChange,
}: ModelPickerTabsProps) {
  const [tab, setTab] = useState<'model' | 'fallback'>('model');

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
  const candidates = useMemo(
    () => orderedModels.filter((m) => !fallbackModels.includes(m)),
    [orderedModels, fallbackModels],
  );

  // A chain is "armed" only when enabled AND non-empty — an empty enabled
  // chain is inert (matches every submit-time guard).
  const fallbackActive = fallbackEnabled && fallbackModels.length > 0;

  const hasDefaultBrand =
    value === null && !!defaultModel && !!getProviderIcon(defaultModel);

  const toggleChain = (model: string) =>
    onFallbackModelsChange(
      fallbackModels.includes(model)
        ? fallbackModels.filter((m) => m !== model)
        : [...fallbackModels, model],
    );

  return (
    <Tabs
      value={tab}
      onValueChange={(v) => setTab(v as 'model' | 'fallback')}
      className="flex min-h-0 flex-1 flex-col"
    >
      <TabsList className="mx-2 mt-2 mb-1.5 shrink-0 self-start">
        <TabsTrigger value="model">Model</TabsTrigger>
        <TabsTrigger value="fallback">
          Fallback
          <span
            aria-hidden="true"
            className={`h-1.5 w-1.5 shrink-0 rounded-full ${
              fallbackActive
                ? 'bg-[hsl(var(--success))]'
                : 'bg-[hsl(var(--destructive))]'
            }`}
          />
          <span className="sr-only">
            {fallbackActive
              ? `, ${fallbackModels.length} model${fallbackModels.length > 1 ? 's' : ''} configured`
              : ', not configured'}
          </span>
        </TabsTrigger>
      </TabsList>

      {/* Strip the vendored input wrapper's own border-b: the row div owns
          ONE full-bleed hairline so the underline reaches the panel edge
          instead of stranding short of the refresh button. */}
      <Command className="flex min-h-0 flex-1 flex-col [&_[cmdk-input-wrapper]]:border-b-0">
        <div className="flex shrink-0 items-center justify-between border-b border-[hsl(var(--border))] pr-2">
          <CommandInput placeholder="Filter models..." className={COMMAND_INPUT_CLS} />
          <RefreshIcon onRefresh={onRefresh} label="Refresh models" />
        </div>
        <ErrorBanner error={error} />

        <TabsContent value="model" className="mt-0 min-h-0">
          <CommandList className="max-h-[300px]">
            <CommandEmpty className={COMMAND_EMPTY_CLS}>
              {loading ? 'Loading...' : 'No matches.'}
            </CommandEmpty>
            {defaultLabel != null && (
              <CommandItem
                value="__default__ Default"
                onSelect={() => onSelect(null)}
                className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${value === null ? 'font-medium' : ''}`}
              >
                <span className="flex items-center gap-1.5">
                  {hasDefaultBrand && defaultModel ? (
                    <ModelBrandIcon modelId={defaultModel} size={14} />
                  ) : (
                    <Cpu className="w-3.5 h-3.5 opacity-60" />
                  )}
                  <span>{defaultLabel}</span>
                  {defaultModel && (
                    <span className="text-xs text-[hsl(var(--muted-foreground))]">
                      {formatModelName(defaultModel)}
                    </span>
                  )}
                </span>
              </CommandItem>
            )}
            {orderedModels.map((model) => {
              const unsupported = isUnsupportedModel(model);
              const isActive = value === model;
              return (
                <CommandItem
                  key={model}
                  value={model}
                  onSelect={() => onSelect(model)}
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
        </TabsContent>

        <TabsContent value="fallback" className="mt-0 flex min-h-0 flex-col">
          <div className="flex shrink-0 items-center justify-between gap-2 px-3 pt-1 pb-1.5">
            <span className="text-xs font-medium text-[hsl(var(--foreground))]">
              Model fallback
            </span>
            <Switch
              checked={fallbackEnabled}
              onCheckedChange={onFallbackEnabledChange}
              aria-label="Enable model fallback"
            />
          </div>
          {fallbackEnabled ? (
            <>
              <div className="flex shrink-0 items-start gap-1.5 px-3 pb-1.5 text-xs leading-snug text-[hsl(var(--muted-foreground))]">
                <AlertTriangle className="w-3 h-3 shrink-0 mt-0.5 text-amber-500" />
                <span>
                  Falling back to another model can change cost, latency, output
                  style, and prompt-cache behaviour. Models are tried in the order
                  shown.
                </span>
              </div>
              <CommandList className="max-h-[240px]">
                {/* Armed chain — pinned above the candidates as plain rows so
                    filtering never re-sorts the priority ordinals. */}
                {fallbackModels.length > 0 && (
                  <div className="flex flex-col gap-1 px-3 py-1">
                    {fallbackModels.map((model, i) => (
                      <FallbackChainRow key={model} model={model} index={i} onRemove={toggleChain} />
                    ))}
                  </div>
                )}
                {candidates.length > 0 && (
                  <>
                    <div className="px-3 pt-1.5 pb-0.5 text-2xs font-medium uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
                      Add to chain
                    </div>
                    {candidates.map((model) => {
                      const unsupported = isUnsupportedModel(model);
                      return (
                        <CommandItem
                          key={model}
                          value={model}
                          disabled={unsupported}
                          onSelect={() => toggleChain(model)}
                          title={unsupported ? 'Not supported for chat or agents' : undefined}
                          className={`${COMMAND_ITEM_SINGLE_LINE_CLS} ${unsupported ? 'text-[hsl(var(--muted-foreground))]' : ''}`}
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
                        </CommandItem>
                      );
                    })}
                  </>
                )}
                {fallbackModels.length === 0 && candidates.length === 0 && (
                  <div className={COMMAND_EMPTY_CLS}>
                    {loading ? 'Loading...' : 'No models available.'}
                  </div>
                )}
              </CommandList>
            </>
          ) : (
            <div className="shrink-0 px-3 pb-3 text-xs leading-snug text-[hsl(var(--muted-foreground))]">
              Turn on to build a retry chain — models are tried in order after
              the primary fails.
            </div>
          )}
        </TabsContent>
      </Command>
    </Tabs>
  );
}
