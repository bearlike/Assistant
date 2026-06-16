import { useEffect, useMemo, useRef, useState } from "react"
import { Check, ChevronDown, Clock, Plus, Search } from "lucide-react"
import { Command as CmdK } from "cmdk"
import {
  CommandGroup,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import {
  ComposerSendButton,
  ComposerShell,
} from "@/components/ui/composer-shell"
import { cn } from "@/lib/utils"
import { RelativeTime } from "../wiki/relativeTime"
import { useTiers } from "../../hooks/useAgenticSearch"
import type { SearchTier, SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { dedupePastQueries, pastQueryKey } from "./utils"
import { SearchScopeControl } from "./SearchScopeControl"

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
          <span className="rounded-full bg-[hsl(var(--primary))] h-1.5 w-1.5 flex-none" />
          <span className="truncate max-w-[72px] sm:max-w-[140px]">{workspace.name}</span>
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
  /** `hero` = the tall landing composer; `compact` = the results-topbar bar.
   *  Both render through the SAME `ComposerShell` — only the size tokens
   *  differ, so the two surfaces read as one component (DRY). */
  variant?: "hero" | "compact"
  /** A run submission is in flight (mutation pending) — submit disables. */
  submitting?: boolean
  /** Source catalog — feeds the scope control's sources footer. */
  sources?: SourceCatalogEntry[]
  onOpenConfig?: (workspace: Workspace) => void
  /** Fast/Auto/Deep budget knob — rendered (in the scope control) when both
   *  props are provided. */
  tier?: SearchTier
  onTierChange?: (tier: SearchTier) => void
  /** Per-run model override ("" = run on the tier's preset). Session-instance-
   *  only by design: the view never persists it (and resets it on a tier
   *  change), so a custom model can be trialled without a config edit. */
  model?: string
  onModelChange?: (model: string) => void
}

/**
 * The single search composer. Both surfaces — the landing hero and the
 * results topbar — render through one `ComposerShell` (the same primitive the
 * Tasks composer uses), differing only by a size token. The toolbar carries
 * exactly two controls: the workspace context pill and the `SearchScopeControl`
 * (tier · model · sources, progressively disclosed). One cmdk `Command`
 * context drives the suggestions dropdown for both.
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
  variant = "compact",
  submitting = false,
  sources = [],
  onOpenConfig,
  tier,
  onTierChange,
  model,
  onModelChange,
}: SearchBarProps) {
  const isHero = variant === "hero"
  // Tier→model presets (config-backed). Feeds the scope control's per-tier
  // model lines AND its resting label, so the control describes the SAME
  // resolution the backend applies (`run.model or tier preset`).
  const tierModels = useTiers().data?.tiers
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
    // Dedupe by normalized query text FIRST so identical reruns collapse to one
    // selectable row (cmdk keys items by `value` — duplicates would otherwise
    // hover/select together). Backend prepends, so the kept entry is the most
    // recent run. Then narrow to the typed needle.
    const past = dedupePastQueries(workspace.past_queries ?? [])
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
            {filtered.map((p, i) => (
              // A recent-query suggestion REPLAYS its stored run (GET snapshot)
              // when it has a run_id + a replay handler — never a fresh POST.
              // Falls back to pre-filling a new run for legacy entries.
              // value/key must be UNIQUE per entry (run_id, else `<q>-<index>`):
              // cmdk identifies items by `value`, so a shared `p.q` made every
              // rerun of a query hover/select as one. Duplicates are already
              // gone (dedupePastQueries), so the index is only a legacy fallback.
              <CommandItem
                key={pastQueryKey(p, i)}
                value={pastQueryKey(p, i)}
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

  // Toolbar — exactly two controls, shared by both variants: the workspace
  // context pill and the progressively-disclosed scope control. The scope
  // control renders only when the run-config props are present (legacy call
  // sites that don't wire tier/model stay valid).
  const toolbarLeft = (
    <>
      <WorkspacePill
        workspace={workspace}
        workspaces={workspaces}
        onPick={onPickWorkspace}
        onNew={onNewWorkspace}
        inline
      />
      {tier && onTierChange && onModelChange && (
        <SearchScopeControl
          tier={tier}
          onTierChange={onTierChange}
          model={model ?? ""}
          onModelChange={onModelChange}
          models={tierModels}
          workspace={workspace}
          sources={sources}
          onOpenConfig={onOpenConfig}
          inline
        />
      )}
    </>
  )

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
      className={cn("block w-full", isHero && "max-w-[720px] mx-auto")}
    >
      <ComposerShell
        wrapRef={wrapRef}
        surface={
          isHero
            ? { radius: "rounded-2xl", elevation: "elev-2", halo: "strong" }
            : { radius: "rounded-xl", elevation: "elev-1", halo: "soft" }
        }
        bodyClassName={cn(
          "relative",
          isHero ? "px-3.5 pt-3.5 pb-2.5 min-h-[96px]" : "px-2.5 pt-2.5 pb-2"
        )}
        top={
          // No expand affordance: cmdk's `Command.Input` is a single-line
          // <input> with no multiline mode, so a faithful "expand to textarea"
          // toggle would mean swapping the element + re-deriving height/Enter
          // handling (>60 LOC). YAGNI — a broken control is worse than none.
          <CmdK.Input
            ref={inputRef}
            value={value}
            onValueChange={(v: string) => {
              onChange(v)
              setAcOpen(true)
            }}
            onFocus={handleFocus}
            placeholder={
              isHero ? "Ask or search the workspace…" : `Search ${workspace.name.toLowerCase()}…`
            }
            className={cn(
              "block w-full bg-transparent border-0 outline-none px-1 text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))]",
              isHero ? "pb-3 text-base" : "pb-2 text-sm"
            )}
          />
        }
        toolbarLeft={toolbarLeft}
        toolbarRight={
          <ComposerSendButton
            onClick={() => submit()}
            submitting={submitting}
            active={Boolean(value.trim())}
            shape="square"
            aria-label={submitting ? "Starting search…" : "Search"}
            className={isHero ? "h-8 w-8 hover:brightness-110 hover:opacity-100" : ""}
          />
        }
        popover={dropdown}
      />
    </CmdK>
  )
}
