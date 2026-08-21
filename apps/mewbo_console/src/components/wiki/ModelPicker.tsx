/**
 * Model picker for the wiki — uses the SAME pattern + primitives as the
 * main chat composer's model tab in `ConfigMenu.tsx`:
 *
 *   - Model list is `string[]` (provider-prefixed IDs, e.g. `anthropic/claude-sonnet-4-5`).
 *   - Brand icons come from `ModelBrandIcon` / `getProviderIcon` (@lobehub/icons).
 *   - Display label uses the shared `formatModelName` helper (strips prefix).
 *   - Unsupported (whisper/embedding) models sink to the bottom of the list
 *     via `isUnsupportedModel`.
 *   - Filter input is shadcn `<CommandInput>` only — no extra Search icon
 *     header (cmdk already shows one).
 *
 * Two trigger variants: `full` (input-shaped, wizard step 2) and `compact`
 * (pill, Q&A dock). The menu body is identical between them.
 */

import { useMemo, useState } from "react";
import { ChevronDown, Cpu } from "lucide-react";

import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";
import { FOCUS_RING } from "@/components/ui/focus-ring";

import { ModelBrandIcon } from "../ModelBrandIcon";
import { ModelPickerTabs } from "../ModelPickerTabs";
import { formatModelName } from "../../utils/model";
import { isUnsupportedModel } from "../../utils/modelSupport";
import { useModels } from "../../hooks/useModels";

interface ModelPickerProps {
  value: string;
  onChange: (modelId: string) => void;
  variant?: "full" | "compact";
  className?: string;
  /** When set, a "Default" row tops the list (the task-composer pattern) and
   *  selecting it emits `""` — the caller's "no override" sentinel. The
   *  trigger shows this label while `value` is empty. */
  defaultLabel?: string;
  /** Native hover hint on the trigger (the repo's `title` idiom — no Tooltip
   *  primitive is vendored). */
  title?: string;
  /** Supplying `fallbackModels` switches the popover from the plain model
   *  list onto the shared tabbed Model | Fallback panel (`ModelPickerTabs`) —
   *  the same mechanism the Tasks composer uses. Omit it for model-only
   *  surfaces (recovery card, Q&A dock). */
  fallbackEnabled?: boolean;
  fallbackModels?: string[];
  onFallbackEnabledChange?: (enabled: boolean) => void;
  onFallbackModelsChange?: (next: string[]) => void;
}

/**
 * The picker's menu body (filter input + Default row + ordered model rows) as
 * a standalone component, so other surfaces can embed the SAME list inside
 * their own container (e.g. the search `SearchScopeControl` hosts it in a
 * DropdownMenuSub — a nested Popover would fight Radix outside-dismiss).
 * Honest extraction: ModelPicker renders exactly this inside its Popover.
 */
export function ModelMenu({
  value,
  onSelect,
  defaultLabel,
}: {
  value: string;
  onSelect: (modelId: string) => void;
  defaultLabel?: string;
}) {
  const { models, loading } = useModels();

  // Order: chat-supported first, unsupported (whisper/embedding) at the
  // bottom. Same logic the main composer applies.
  const ordered = useMemo(
    () =>
      [...models].sort(
        (a, b) => (isUnsupportedModel(a) ? 1 : 0) - (isUnsupportedModel(b) ? 1 : 0)
      ),
    [models]
  );

  const isDefault = !value && defaultLabel != null;
  return (
    <Command>
      <CommandInput placeholder="Filter models…" className="h-7 py-1" />
      <CommandList className="max-h-[320px]">
        <CommandEmpty className="py-3 text-center text-xs text-[hsl(var(--muted-foreground))]">
          {loading ? "Loading…" : "No matches."}
        </CommandEmpty>
        <CommandGroup>
          {defaultLabel != null && (
            <CommandItem
              value="__default__"
              onSelect={() => onSelect("")}
              className={cn(
                "flex items-center gap-2 px-3 py-1.5 text-sm rounded-none cursor-pointer aria-selected:bg-[hsl(var(--accent))]",
                isDefault && "font-medium"
              )}
            >
              <Cpu className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />
              <span className="truncate">{defaultLabel}</span>
            </CommandItem>
          )}
          {ordered.map((id) => {
            const unsupported = isUnsupportedModel(id);
            return (
              <CommandItem
                key={id}
                value={id}
                onSelect={() => onSelect(id)}
                title={unsupported ? "Not supported for chat or agents" : undefined}
                className={cn(
                  "flex items-center gap-2 px-3 py-1.5 text-sm rounded-none cursor-pointer aria-selected:bg-[hsl(var(--accent))]",
                  value === id && "font-medium",
                  unsupported && "text-[hsl(var(--muted-foreground))]"
                )}
              >
                <ModelBrandIcon modelId={id} size={14} />
                <span className="truncate">{formatModelName(id)}</span>
                {id.includes("/") && (
                  <span className="text-2xs text-[hsl(var(--muted-foreground))] truncate ml-auto">
                    {id}
                  </span>
                )}
              </CommandItem>
            );
          })}
        </CommandGroup>
      </CommandList>
    </Command>
  );
}

export function ModelPicker({
  value,
  onChange,
  variant = "full",
  className,
  defaultLabel,
  title,
  fallbackEnabled,
  fallbackModels,
  onFallbackEnabledChange,
  onFallbackModelsChange,
}: ModelPickerProps) {
  const [open, setOpen] = useState(false);
  const pick = (id: string) => {
    onChange(id);
    setOpen(false);
  };
  // Presence of the full fallback wiring, not a separate boolean prop,
  // decides the popover shape — a caller either wants the whole mechanism or
  // none of it, so there is no state where these could disagree.
  const { models, loading, error, refresh } = useModels();
  const tabBody =
    fallbackModels != null && onFallbackEnabledChange != null && onFallbackModelsChange != null ? (
      <ModelPickerTabs
        models={models}
        value={value || null}
        onSelect={(id) => pick(id ?? "")}
        defaultLabel={defaultLabel}
        loading={loading}
        error={error}
        onRefresh={refresh}
        fallbackEnabled={!!fallbackEnabled}
        fallbackModels={fallbackModels}
        onFallbackEnabledChange={onFallbackEnabledChange}
        onFallbackModelsChange={onFallbackModelsChange}
      />
    ) : null;
  const fallbackActive = tabBody != null && !!fallbackEnabled && (fallbackModels?.length ?? 0) > 0;

  // `min-w-0` + `whitespace-nowrap` + a max-width on the compact pill
  // prevents the model id from wrapping or overflowing the dock row when a
  // longer id like `anthropic/claude-sonnet-4-5` is selected.
  const triggerClass =
    variant === "compact"
      ? "inline-flex items-center gap-1.5 px-2.5 h-7 max-w-[220px] rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--card))] text-xs text-[hsl(var(--foreground))] whitespace-nowrap hover:bg-[hsl(var(--accent))] transition-colors"
      : `inline-flex items-center gap-2 px-3 h-11 w-full rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] text-sm text-[hsl(var(--foreground))] whitespace-nowrap hover:border-[hsl(var(--border-strong))] ${FOCUS_RING} transition-colors`;

  const isDefault = !value && defaultLabel != null;
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(triggerClass, className)}
          aria-haspopup="listbox"
          title={title}
        >
          {isDefault ? (
            <Cpu
              className="shrink-0 text-[hsl(var(--muted-foreground))]"
              style={{ width: variant === "compact" ? 12 : 14, height: variant === "compact" ? 12 : 14 }}
            />
          ) : (
            <ModelBrandIcon modelId={value} size={variant === "compact" ? 12 : 14} />
          )}
          <span
            className={cn(
              "truncate min-w-0 flex-1 text-left",
              variant === "compact" ? "text-2xs" : "text-xs"
            )}
          >
            {isDefault ? defaultLabel : formatModelName(value)}
          </span>
          {fallbackActive && (
            <span className="shrink-0 min-w-[16px] h-4 px-1 inline-flex items-center justify-center rounded-full bg-[hsl(var(--primary))]/15 text-2xs font-medium text-[hsl(var(--primary-text))]">
              +{fallbackModels?.length}
            </span>
          )}
          <ChevronDown className="h-3 w-3 text-[hsl(var(--muted-foreground))] shrink-0" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        sideOffset={6}
        collisionPadding={16}
        className={cn(
          "p-0 rounded-lg border-[hsl(var(--border-strong))] bg-[hsl(var(--popover))] overflow-hidden",
          tabBody ? "w-80 max-h-[460px] flex flex-col" : "w-[340px] max-w-[calc(100vw-2rem)]",
        )}
      >
        {tabBody ?? <ModelMenu value={value} onSelect={pick} defaultLabel={defaultLabel} />}
      </PopoverContent>
    </Popover>
  );
}

/**
 * Inline `[brand-icon] model-id` badge used by the QA page's "Generated
 * with" affordance and the wizard summary strip. Identical visual + helper
 * stack as the chat composer's read-only model label.
 */
export function ModelChip({
  modelId,
  className,
}: {
  modelId: string;
  className?: string;
}) {
  return (
    <span className={cn("inline-flex items-center gap-1.5", className)}>
      <ModelBrandIcon modelId={modelId} size={12} />
      <span className="text-xs text-[hsl(var(--foreground))]">
        {formatModelName(modelId)}
      </span>
    </span>
  );
}
