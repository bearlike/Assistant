import { useState } from "react"
import { ChevronDown, Cpu, Gauge, Layers, SlidersHorizontal } from "lucide-react"

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"
import { ModelBrandIcon } from "../ModelBrandIcon"
import { ModelFallbackChain } from "../ModelFallbackChain"
import { useModels } from "../../hooks/useModels"
import { ModelMenu } from "../wiki/ModelPicker"
import { formatModelName } from "../../utils/model"
import type {
  SearchTier,
  SearchTiersInfo,
  SourceCatalogEntry,
  Workspace,
} from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"
import { TIERS } from "./tiers"

/**
 * The run-config trio — budget tier + model override, plus their setters — as
 * ONE prop threaded `AgenticSearchView` → `ResultsPanel`/`LandingPanel` →
 * `SearchBar` → here, instead of four individually-drilled props at every
 * layer. A new run-config knob is one field here, not a new prop at each hop.
 */
export interface SearchScope {
  tier: SearchTier
  onTierChange: (tier: SearchTier) => void
  /** Per-run model override ("" = the tier's preset). */
  model: string
  onModelChange: (model: string) => void
  /** Ordered cross-model fallback ladder for this run. Empty = no ladder;
   *  there is no separate "enabled" wire field. */
  fallbackModels: string[]
  onFallbackModelsChange: (next: string[]) => void
}

interface SearchScopeControlProps {
  scope: SearchScope
  /** Per-tier model presets from `GET /tiers`; rows omit the model line
   *  until it resolves (never a fabricated placeholder). */
  models?: SearchTiersInfo["tiers"]
  /** Workspace + catalog feed the sources row; the row renders only when an
   *  `onOpenConfig` handler is provided. */
  workspace: Workspace
  sources?: SourceCatalogEntry[]
  onOpenConfig?: (workspace: Workspace) => void
  /** Transparent hero-footer treatment (the bar's border is the container). */
  inline?: boolean
}

/**
 * THE one run-configuration pill. Collapses the former TierPill +
 * model-override pill + Configure chip into a single progressive-disclosure
 * control so the composer keeps exactly two pills: workspace (identity) and
 * scope (everything about HOW the run executes).
 *
 * The trigger stays honest at rest — it names the tier AND the model that
 * will actually drive the run (`override or the tier's preset`), e.g.
 * "Auto · claude-sonnet-4-6". One DropdownMenu hosts all three concerns
 * (tier radio rows → model sub-menu → sources row); a nested Popover would
 * fight Radix outside-dismiss, a sub-menu doesn't.
 */
export function SearchScopeControl({
  scope,
  models,
  workspace,
  sources = [],
  onOpenConfig,
  inline,
}: SearchScopeControlProps) {
  const { tier, onTierChange, model, onModelChange, fallbackModels, onFallbackModelsChange } =
    scope
  const { models: allModels } = useModels()
  // Controlled so a model pick inside the cmdk sub-menu (not a DropdownMenuItem,
  // so Radix doesn't auto-close on select) can close the whole menu.
  const [open, setOpen] = useState(false)
  // The switch is UI-only: an armed-but-empty ladder is inert, and turning it
  // off clears the list so the wire keeps its one meaning (a non-empty list IS
  // the ladder).
  const [fallbackOn, setFallbackOn] = useState(fallbackModels.length > 0)
  const current = TIERS.find((t) => t.id === tier) ?? TIERS[1]
  // The resolution the backend applies: `run.model or the tier's preset`.
  const resolved = model || models?.[tier] || ""
  const label = resolved ? `${current.name} · ${formatModelName(resolved)}` : current.name

  const wsSourceObjs = workspace.sources
    .map((id) => sources.find((s) => s.id === id))
    .filter((s): s is SourceCatalogEntry => Boolean(s))
    .slice(0, 4)

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          title="Search scope — budget tier, model & sources for this run"
          aria-label="Search scope"
          className={cn(
            "inline-flex items-center gap-1.5 transition-colors flex-none min-w-0 rounded-md font-medium",
            inline
              ? "h-[30px] px-2.5 text-sm text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              : "h-8 px-2.5 text-sm hover:bg-[hsl(var(--accent))]"
          )}
        >
          <Gauge className="h-3 w-3 flex-none opacity-70" />
          <span className="truncate max-w-[78px] sm:max-w-[180px]">{label}</span>
          {fallbackOn && fallbackModels.length > 0 && (
            <span className="flex-none text-2xs text-[hsl(var(--muted-foreground))]">
              +{fallbackModels.length}
            </span>
          )}
          <ChevronDown className="h-3 w-3 flex-none opacity-60" />
        </button>
      </DropdownMenuTrigger>
      {/* Narrower than the full 18rem on phone-width viewports so the model
          sub-flyout has room to flip onto a side that stays on-screen (a
          nested side-menu can't coexist with a full-width parent at <400px). */}
      <DropdownMenuContent
        align="start"
        collisionPadding={16}
        className="w-[min(18rem,calc(100vw-8rem))] [box-shadow:var(--elev-3)]"
      >
        <DropdownMenuLabel className="text-2xs font-normal text-[hsl(var(--muted-foreground))]">
          Search budget — depth · fan-out · model
        </DropdownMenuLabel>
        <DropdownMenuRadioGroup
          value={tier}
          onValueChange={(v) => onTierChange(v as SearchTier)}
        >
          {/* Two-line rows: line 1 = name + the budget it buys (prose hint as
              a consistent right column); line 2 = the tier's model preset,
              muted rather than mono — a model name is a name, not machine
              text — so "the tier picks the brain" is visible before
              committing. The radio indicator owns the reserved left slot
              (pl-8 from the primitive). */}
          {TIERS.map((t) => {
            const preset = models?.[t.id]
            return (
              <DropdownMenuRadioItem key={t.id} value={t.id} className="py-1.5">
                <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                  <span className="flex items-center">
                    <span className="font-medium">{t.name}</span>
                    <span className="ml-auto pl-3 text-xs text-[hsl(var(--muted-foreground))]">
                      {t.hint}
                    </span>
                  </span>
                  {preset && (
                    <span className="flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
                      <ModelBrandIcon modelId={preset} size={10} />
                      <span className="truncate">{formatModelName(preset)}</span>
                    </span>
                  )}
                </span>
              </DropdownMenuRadioItem>
            )
          })}
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        {/* Per-run model override. Session-instance-only by design (the view
            never persists it and a tier pick clears it) — trial a custom
            model without a config edit or server restart. */}
        <DropdownMenuSub>
          <DropdownMenuSubTrigger className="gap-2" title="Model for this run — overrides the tier's preset">
            {model ? (
              <ModelBrandIcon modelId={model} size={12} />
            ) : (
              <Cpu className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
            )}
            <span className="flex-1 text-sm">Model</span>
            <span className="text-2xs text-[hsl(var(--muted-foreground))] truncate max-w-[120px]">
              {model ? formatModelName(model) : "tier preset"}
            </span>
          </DropdownMenuSubTrigger>
          {/* A nested side-flyout can't sit beside the parent menu on a phone-
              width viewport, so bind its width to Radix's measured available
              width (the space on the side it flips to, collision-padding
              already subtracted) — it shrinks to stay fully on-screen instead
              of overflowing the edge, and the ModelMenu rows truncate. Desktop
              keeps the full 340px. `min-w-0` is required: the primitive's
              `min-w-[8rem]` would otherwise floor the width and re-overflow. */}
          <DropdownMenuSubContent
            collisionPadding={16}
            className="w-[min(340px,var(--radix-dropdown-menu-content-available-width))] min-w-0 p-0 overflow-hidden"
          >
            <ModelMenu
              value={model}
              onSelect={(id) => {
                onModelChange(id)
                setOpen(false)
              }}
              defaultLabel={`${current.name} preset${
                models?.[tier] ? ` · ${formatModelName(models[tier])}` : ""
              }`}
            />
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        {/* Cross-model fallback for this run. Same width clamp + `min-w-0` as
            the model flyout — the primitive's own `min-w-[8rem]` would
            otherwise floor the width and re-overflow on a phone. */}
        <DropdownMenuSub>
          <DropdownMenuSubTrigger
            className="gap-2"
            title="Models to try if the one driving this run keeps failing"
          >
            <Layers className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
            <span className="flex-1 text-sm">Fallback</span>
            <span className="text-2xs text-[hsl(var(--muted-foreground))] truncate max-w-[120px]">
              {fallbackOn && fallbackModels.length > 0
                ? `${fallbackModels.length} model${fallbackModels.length > 1 ? "s" : ""}`
                : "off"}
            </span>
          </DropdownMenuSubTrigger>
          <DropdownMenuSubContent
            collisionPadding={16}
            className="w-[min(340px,var(--radix-dropdown-menu-content-available-width))] min-w-0 p-0 overflow-hidden"
          >
            <ModelFallbackChain
              models={allModels}
              value={fallbackModels}
              onChange={onFallbackModelsChange}
              enabled={fallbackOn}
              onEnabledChange={(on) => {
                setFallbackOn(on)
                if (!on) onFallbackModelsChange([])
              }}
              className=""
            />
          </DropdownMenuSubContent>
        </DropdownMenuSub>
        {onOpenConfig && (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              onSelect={() => onOpenConfig(workspace)}
              title="Configure workspace sources"
              className="gap-2"
            >
              <SlidersHorizontal className="h-3 w-3 text-[hsl(var(--muted-foreground))]" />
              <span className="flex-1 text-sm">Sources</span>
              {wsSourceObjs.length > 0 ? (
                <span className="inline-flex gap-1">
                  {wsSourceObjs.map((s) => (
                    <SrcAvatar key={s.id} source={s} size={14} />
                  ))}
                </span>
              ) : (
                <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                  {workspace.sources.length || "none"}
                </span>
              )}
            </DropdownMenuItem>
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
