import { useEffect, useMemo, useRef, useState } from "react"
import {
  Check,
  ChevronDown,
  Clock,
  Gauge,
  Plus,
  Search,
  SlidersHorizontal,
} from "lucide-react"
import { Command as CmdK } from "cmdk"
import {
  CommandGroup,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  ComposerSendButton,
  ComposerShell,
  composerSurface,
  composerSurfaceData,
} from "@/components/ui/composer-shell"
import { cn } from "@/lib/utils"
import { RelativeTime } from "../wiki/relativeTime"
import type { SearchTier, SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

// Tier = one budget knob (decomposition depth + probe fan-out) — see
// docs/features-search.md "Search tiers". Default is auto.
const TIERS: { id: SearchTier; name: string; hint: string }[] = [
  { id: "fast", name: "Fast", hint: "quick lookups" },
  { id: "auto", name: "Auto", hint: "balanced (default)" },
  { id: "deep", name: "Deep", hint: "exhaustive research" },
]

/** Fast/Auto/Deep selector — same pill language as `WorkspacePill`. */
function TierPill({
  tier,
  onChange,
  inline,
}: {
  tier: SearchTier
  onChange: (tier: SearchTier) => void
  inline?: boolean
}) {
  const current = TIERS.find((t) => t.id === tier) ?? TIERS[1]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button
          type="button"
          title="Search tier"
          aria-label="Search tier"
          className={cn(
            "inline-flex items-center gap-1.5 transition-colors flex-none rounded-md font-medium",
            inline
              ? "h-[30px] px-2.5 text-[12.5px] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              : "h-8 px-2.5 text-sm hover:bg-[hsl(var(--accent))]"
          )}
        >
          <Gauge className="h-3 w-3 opacity-70" />
          <span>{current.name}</span>
          <ChevronDown className="h-3 w-3 opacity-60" />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-56 shadow-[var(--elev-3)]">
        <DropdownMenuRadioGroup
          value={tier}
          onValueChange={(v) => onChange(v as SearchTier)}
        >
          {/* Uniform-height rows: the radio indicator owns the reserved left
              slot (pl-8 from the primitive); the name fills, and the prose
              hint is a consistent right column — NOT mono (it's prose, not
              data), so the rows read evenly sized regardless of hint length. */}
          {TIERS.map((t) => (
            <DropdownMenuRadioItem key={t.id} value={t.id} className="h-8">
              <span className="font-medium">{t.name}</span>
              <span className="ml-auto pl-3 text-xs text-[hsl(var(--muted-foreground))]">
                {t.hint}
              </span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/**
 * Workspace selector. Two visual modes — chip (default) for the compact
 * results-topbar bar, and inline (transparent) for the hero footer where
 * the bar's own border is the container.
 */
interface WorkspacePillProps {
  workspace: Workspace
  workspaces: Workspace[]
  onPick: (workspace: Workspace) => void
  onNew: () => void
  inline?: boolean
}

function WorkspacePill({ workspace, workspaces, onPick, onNew, inline }: WorkspacePillProps) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-2 transition-colors flex-none rounded-md font-medium",
            inline
              ? "h-[30px] px-2.5 text-[12.5px] text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              : "h-8 px-2.5 text-sm hover:bg-[hsl(var(--accent))]"
          )}
        >
          <span
            className={cn(
              "rounded-full bg-[hsl(var(--primary))]",
              inline ? "h-1.5 w-1.5" : "h-1.5 w-1.5"
            )}
          />
          <span className="truncate max-w-[140px]">{workspace.name}</span>
          <ChevronDown className="h-3 w-3 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-1 shadow-[var(--elev-3)]">
        <div className="px-2 py-1.5 text-[11px] uppercase tracking-wider text-[hsl(var(--muted-foreground))] font-mono">
          Your workspaces
        </div>
        <ul className="space-y-0.5">
          {workspaces.map((w) => (
            <li key={w.id}>
              <button
                type="button"
                onClick={() => {
                  onPick(w)
                  setOpen(false)
                }}
                className={cn(
                  "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm hover:bg-[hsl(var(--accent))] transition-colors",
                  w.id === workspace.id && "bg-[hsl(var(--accent))]"
                )}
              >
                <span className="flex-1 truncate">{w.name}</span>
                <span className="text-[11px] text-[hsl(var(--muted-foreground))] font-mono">
                  {w.sources.length} sources
                </span>
                {w.id === workspace.id && (
                  <Check className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
                )}
              </button>
            </li>
          ))}
        </ul>
        <div className="border-t border-[hsl(var(--border))] mt-1 pt-1">
          <button
            type="button"
            onClick={() => {
              setOpen(false)
              onNew()
            }}
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm text-[hsl(var(--primary))] hover:bg-[hsl(var(--accent))]"
          >
            <Plus className="h-3.5 w-3.5" />
            New workspace
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

interface SearchBarProps {
  value: string
  onChange: (value: string) => void
  onSubmit: (value: string) => void
  /** Replay a stored run by id (GET snapshot) — past-query suggestions use this
   *  instead of re-running. Optional so legacy call sites stay valid; when
   *  absent a past-query item falls back to pre-filling a fresh run. */
  onReplay?: (runId: string) => void
  workspace: Workspace
  workspaces: Workspace[]
  onPickWorkspace: (workspace: Workspace) => void
  onNewWorkspace: () => void
  autoFocus?: boolean
  compact?: boolean
  /** A run submission is in flight (mutation pending) — submit disables. */
  submitting?: boolean
  /** "hero" → two-row 96px bar matching the task-landing rhythm. */
  variant?: "hero"
  /** Hero footer Configure pill — shows up to 4 source avatars + sliders icon. */
  sources?: SourceCatalogEntry[]
  onOpenConfig?: (workspace: Workspace) => void
  /** Fast/Auto/Deep budget knob — rendered when both props are provided. */
  tier?: SearchTier
  onTierChange?: (tier: SearchTier) => void
}

/**
 * Search affordance with three visual modes — `hero` (two-row 96px landing),
 * `compact` (results topbar), and the default single-row bar. All modes
 * share one cmdk Command context so keyboard nav stays connected, plus the
 * elevation tokens (`--elev-1..3`) from `index.css`.
 */
export function SearchBar({
  value,
  onChange,
  onSubmit,
  onReplay,
  workspace,
  workspaces,
  onPickWorkspace,
  onNewWorkspace,
  autoFocus = false,
  compact = false,
  submitting = false,
  variant,
  sources = [],
  onOpenConfig,
  tier,
  onTierChange,
}: SearchBarProps) {
  const isHero = variant === "hero"
  const [acOpen, setAcOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  // The mount-time `autoFocus` focus must NOT pop the suggestions open — the
  // dropdown opens on a genuine user focus/typing gesture only (#82). We
  // suppress the open-on-focus for exactly the one programmatic focus call.
  const suppressFocusOpenRef = useRef(false)

  useEffect(() => {
    if (autoFocus) {
      suppressFocusOpenRef.current = true
      inputRef.current?.focus()
    }
  }, [autoFocus])

  // Open the suggestions when the input takes focus — but skip the single
  // programmatic focus fired by `autoFocus` on mount (which would otherwise
  // render `combobox [expanded]` before any interaction).
  const handleFocus = () => {
    if (suppressFocusOpenRef.current) {
      suppressFocusOpenRef.current = false
      return
    }
    setAcOpen(true)
  }

  // Close autocomplete on outside click. cmdk's Command doesn't ship a
  // controlled-open story for an external dropdown, so this thin doc
  // listener is the practical seam.
  useEffect(() => {
    if (!acOpen) return
    const onDoc = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setAcOpen(false)
      }
    }
    document.addEventListener("mousedown", onDoc)
    return () => document.removeEventListener("mousedown", onDoc)
  }, [acOpen])

  const filtered = useMemo(() => {
    const past = workspace.past_queries ?? []
    if (!value.trim()) return past
    const needle = value.toLowerCase()
    return past.filter((p) => p.q.toLowerCase().includes(needle))
  }, [value, workspace.past_queries])
  const otherWorkspaces = useMemo(
    () => workspaces.filter((w) => w.id !== workspace.id).slice(0, 3),
    [workspaces, workspace.id]
  )

  const submit = (override?: string) => {
    const q = (override ?? value).trim()
    if (!q || submitting) return
    setAcOpen(false)
    onSubmit(q)
  }

  const hasContent =
    filtered.length > 0 || (!!value.trim() && filtered.length === 0) || otherWorkspaces.length > 0

  // Suggestions dropdown — a Google-suggest-style extension of the composer
  // surface. It anchors tight to the bar (mt-1, same border-strong + radius
  // family + elev-3) and pans out from beneath it via the `.composer-suggest`
  // origin-top entrance (index.css, reduced-motion safe). One typographic
  // scale: group headings (cmdk `text-xs` muted), item label (text-sm), and a
  // consistent right meta column that keeps `font-mono` ONLY on data
  // (counts/times). Icons are uniform 14px (`[&_svg]:size-3.5`) at one opacity.
  const dropdown = acOpen && hasContent && (
    <div
      className="composer-suggest absolute left-0 right-0 top-full mt-1 z-40 rounded-xl border border-[hsl(var(--border-strong))] bg-[hsl(var(--popover))] shadow-[var(--elev-3)] overflow-hidden"
      onMouseDown={(e) => e.preventDefault()}
    >
      <CommandList className="[&_[cmdk-item]_svg]:size-3.5">
        {filtered.length === 0 && !!value.trim() && (
          // Empty "search this" row — same icon size/opacity + gap rhythm as
          // the item rows so it reads as a peer, not a one-off.
          <CommandGroup heading={workspace.name}>
            <CommandItem
              value={`__search__${value}`}
              onSelect={() => submit(value)}
              className="gap-2.5"
            >
              <Search className="opacity-60" />
              <span className="flex-1 truncate">
                Search <span className="text-[hsl(var(--foreground))] font-medium">"{value}"</span>
              </span>
            </CommandItem>
          </CommandGroup>
        )}
        {filtered.length > 0 && (
          <CommandGroup heading={`Recent in ${workspace.name}`}>
            {filtered.map((p) => (
              // A recent-query suggestion REPLAYS its stored run (GET snapshot)
              // when it has a run_id + a replay handler — never a fresh POST.
              // Falls back to pre-filling a new run for legacy entries.
              <CommandItem
                key={p.q}
                value={p.q}
                onSelect={() => {
                  if (p.run_id && onReplay) {
                    setAcOpen(false)
                    onReplay(p.run_id)
                  } else {
                    submit(p.q)
                  }
                }}
                className="gap-2.5"
              >
                <Clock className="opacity-60" />
                <span className="flex-1 truncate">{p.q}</span>
                <span className="flex-none pl-3 text-[11px] font-mono tabular-nums text-[hsl(var(--muted-foreground))]">
                  {/* Data right-column — mono is meaningful here (count · time).
                      Relative label computed FE-side from the ISO field; the
                      server-formatted `when` only covers un-migrated rows. */}
                  {p.results} · {p.ran_at ? RelativeTime.format(p.ran_at) : p.when}
                </span>
              </CommandItem>
            ))}
          </CommandGroup>
        )}
        {otherWorkspaces.length > 0 && (
          <>
            <CommandSeparator />
            <CommandGroup heading="Switch workspace">
              {otherWorkspaces.map((w) => (
                <CommandItem
                  key={w.id}
                  value={`ws-${w.id}`}
                  onSelect={() => {
                    setAcOpen(false)
                    onPickWorkspace(w)
                  }}
                  className="gap-2.5"
                >
                  {/* Workspace dot occupies the same 14px icon slot as the
                      Clock/Search glyphs so the gap rhythm stays uniform. */}
                  <span className="flex h-3.5 w-3.5 items-center justify-center">
                    <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))]" />
                  </span>
                  <span className="flex-1 truncate">{w.name}</span>
                  <span className="flex-none pl-3 text-[11px] font-mono tabular-nums text-[hsl(var(--muted-foreground))]">
                    {w.sources.length} sources
                  </span>
                </CommandItem>
              ))}
            </CommandGroup>
          </>
        )}
      </CommandList>
    </div>
  )

  if (isHero) {
    const wsSourceObjs = workspace.sources
      .map((id) => sources.find((s) => s.id === id))
      .filter((s): s is SourceCatalogEntry => Boolean(s))
      .slice(0, 4)
    return (
      <CmdK
        shouldFilter={false}
        loop
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (!acOpen || filtered.length === 0) {
              e.preventDefault()
              submit()
            }
          } else if (e.key === "Escape") {
            setAcOpen(false)
          }
        }}
        className="block w-full max-w-[720px] mx-auto"
      >
        <ComposerShell
          wrapRef={wrapRef}
          surface={{ radius: "rounded-2xl", elevation: "elev-2", halo: "strong" }}
          bodyClassName="relative px-3.5 pt-3.5 pb-2.5 min-h-[96px]"
          top={
            // No expand affordance here: cmdk's `Command.Input` is a
            // single-line <input> with no multiline mode, so a faithful
            // "expand to a textarea" toggle would mean swapping the input
            // element + re-deriving Enter/newline/height handling (>60 LOC of
            // fiddly state). YAGNI — the hero box is a single-line natural-
            // language prompt; a broken Maximize button was worse than none.
            <CmdK.Input
              ref={inputRef}
              value={value}
              onValueChange={(v: string) => {
                onChange(v)
                setAcOpen(true)
              }}
              onFocus={handleFocus}
              placeholder="Ask or search the workspace…"
              className="block w-full bg-transparent border-0 outline-none px-1 pb-3 text-base text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))]"
            />
          }
          toolbarLeft={
            <>
              {/* No Attach/Voice affordances: like the removed Expand button,
                  a control that does nothing is worse than none. Re-add only
                  alongside a real implementation. */}
              <WorkspacePill
                workspace={workspace}
                workspaces={workspaces}
                onPick={onPickWorkspace}
                onNew={onNewWorkspace}
                inline
              />
              {tier && onTierChange && (
                <TierPill tier={tier} onChange={onTierChange} inline />
              )}
              {onOpenConfig && wsSourceObjs.length > 0 && (
                <button
                  type="button"
                  onClick={() => onOpenConfig(workspace)}
                  title="Configure workspace sources"
                  className="hidden sm:inline-flex items-center gap-2 h-[30px] px-2.5 rounded-md text-xs text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
                >
                  <span className="inline-flex gap-1">
                    {wsSourceObjs.map((s) => (
                      <SrcAvatar key={s.id} source={s} size={16} />
                    ))}
                  </span>
                  <SlidersHorizontal className="h-3 w-3" />
                </button>
              )}
            </>
          }
          toolbarRight={
            <ComposerSendButton
              onClick={() => submit()}
              submitting={submitting}
              active={Boolean(value.trim())}
              shape="square"
              aria-label={submitting ? "Starting search…" : "Search"}
              className="h-8 w-8 hover:brightness-110 hover:opacity-100"
            />
          }
          popover={dropdown}
        />
      </CmdK>
    )
  }

  // Default + compact (results topbar): single-row bar, retained as-is for the
  // existing call sites. Picks up the elevation tokens for visual consistency.
  return (
    <div ref={wrapRef} className="relative w-full">
      <CmdK
        shouldFilter={false}
        loop
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            if (!acOpen || filtered.length === 0) {
              e.preventDefault()
              submit()
            }
          } else if (e.key === "Escape") {
            setAcOpen(false)
          }
        }}
        className="block"
      >
        <div
          className={cn(
            // px-2 so the send button on the right has the same breathing
            // room as the workspace pill on the left, matching the button's
            // 8/9px top/bottom inset.
            "flex items-center gap-1 px-2",
            composerSurface({ radius: "rounded-full", elevation: "elev-1", halo: "soft" }),
            compact ? "h-11" : "h-[54px]"
          )}
          {...composerSurfaceData({ halo: "soft" })}
        >
          <WorkspacePill
            workspace={workspace}
            workspaces={workspaces}
            onPick={onPickWorkspace}
            onNew={onNewWorkspace}
          />
          {tier && onTierChange && <TierPill tier={tier} onChange={onTierChange} />}
          <span aria-hidden className="h-5 w-px bg-[hsl(var(--border))] mx-1" />
          <CmdK.Input
            ref={inputRef}
            value={value}
            onValueChange={(v: string) => {
              onChange(v)
              setAcOpen(true)
            }}
            onFocus={handleFocus}
            placeholder={
              compact
                ? `Search ${workspace.name.toLowerCase()}…`
                : "Ask or search the workspace…"
            }
            className={cn(
              "flex-1 bg-transparent border-0 outline-none px-1 text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))]",
              compact ? "h-10 text-sm" : "h-12 text-base"
            )}
          />
          {/* Smaller than the bar by ~16px so there's a visible inset on top,
              bottom, AND right (matching the px-2 padding above). Square in
              compact mode, round otherwise so it echoes the pill bar's curve. */}
          <ComposerSendButton
            onClick={() => submit()}
            submitting={submitting}
            active={Boolean(value.trim())}
            shape={compact ? "square" : "round"}
            aria-label={submitting ? "Starting search…" : "Search"}
          />
        </div>
        {dropdown}
      </CmdK>
    </div>
  )
}
