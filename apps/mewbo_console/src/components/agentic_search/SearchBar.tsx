import { useEffect, useMemo, useRef, useState } from "react"
import { Command as CmdK } from "cmdk"
import {
  ComposerSendButton,
  composerInputCls,
  ComposerShell,
} from "@/components/ui/composer-shell"
import { cn } from "@/lib/utils"
import { useTiers } from "../../hooks/useAgenticSearch"
import type { SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { dedupePastQueries } from "./utils"
import { SearchScopeControl, type SearchScope } from "./SearchScopeControl"
import { SearchSuggestions } from "./SearchSuggestions"
import { WorkspacePill } from "./WorkspacePill"

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
  onSelectWorkspace: (workspace: Workspace) => void
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
  /** Fast/Auto/Deep budget + model override — rendered (in the scope control)
   *  when provided; absent for legacy call sites that don't wire run config. */
  scope?: SearchScope
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
  onSelectWorkspace,
  onNewWorkspace,
  autoFocus = false,
  variant = "compact",
  submitting = false,
  sources = [],
  onOpenConfig,
  scope,
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
  // dropdown opens on a genuine user focus/typing gesture only. We
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

  // Close autocomplete on outside click. VERIFIED reason (not "cmdk can't do
  // a controlled Popover" — ModelSelector and ConfigMenu already nest a cmdk
  // `Command` inside a controlled `Popover` successfully, so that claim was
  // wrong): the blocker is layout, not cmdk. The suggestions strip must
  // anchor to the FULL composer bar width (`left-0 right-0` against the
  // whole bar), but a Radix `PopoverTrigger` sizes/positions `PopoverContent`
  // off its OWN element — the input alone is too narrow, and making the
  // whole bar the trigger would fight the toolbar's OWN popovers
  // (WorkspacePill / SearchScopeControl) over click-to-toggle. A plain doc
  // listener + open-on-focus satisfies both constraints; a Popover doesn't.
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

  // The dropdown's Recent/Switch-workspace selections must close the
  // suggestions before delegating — wrap once here so `SearchSuggestions`
  // stays a pure prop-in/callback-out component with no acOpen access.
  const handleSuggestReplay = onReplay
    ? (runId: string) => {
        setAcOpen(false)
        onReplay(runId)
      }
    : undefined
  const handleSuggestPickWorkspace = (w: Workspace) => {
    setAcOpen(false)
    onSelectWorkspace(w)
  }

  // Toolbar — exactly two controls, shared by both variants: the workspace
  // context pill and the progressively-disclosed scope control. The scope
  // control renders only when the run-config props are present (legacy call
  // sites that don't wire tier/model stay valid).
  const toolbarLeft = (
    <>
      <WorkspacePill
        workspace={workspace}
        workspaces={workspaces}
        onSelect={onSelectWorkspace}
        onNew={onNewWorkspace}
        inline
      />
      {scope && (
        <SearchScopeControl
          scope={scope}
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
            ? { elevation: "elev-2", halo: "strong" }
            : { elevation: "elev-1", halo: "soft" }
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
              // `composerInputCls()` — always `text-field`, a composer
              // input's size full stop, hero-vs-compact is padding only.
              // This was previously a raw `text-base`/`text-sm` ternary, both
              // under the 16px iOS zoom floor, and invisible to the
              // typography guard test because cmdk's `<Command.Input>`
              // doesn't match its `<input>`/`<Input>` element scan.
              composerInputCls(),
              isHero ? "pb-3" : "pb-2"
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
        popover={
          <SearchSuggestions
            acOpen={acOpen}
            filtered={filtered}
            otherWorkspaces={otherWorkspaces}
            value={value}
            workspace={workspace}
            onSubmit={submit}
            onReplay={handleSuggestReplay}
            onSelectWorkspace={handleSuggestPickWorkspace}
          />
        }
      />
    </CmdK>
  )
}
