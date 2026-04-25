import { useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowUp,
  Check,
  ChevronDown,
  Clock,
  Maximize2,
  Mic,
  Plus,
  Search,
  SlidersHorizontal,
} from "lucide-react"
import { Command as CmdK } from "cmdk"
import {
  CommandEmpty,
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
import { cn } from "@/lib/utils"
import type { SourceCatalogEntry, Workspace } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

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
  workspace: Workspace
  workspaces: Workspace[]
  onPickWorkspace: (workspace: Workspace) => void
  onNewWorkspace: () => void
  autoFocus?: boolean
  compact?: boolean
  /** "hero" → two-row 96px bar matching the task-landing rhythm. */
  variant?: "hero"
  /** Hero footer Configure pill — shows up to 4 source avatars + sliders icon. */
  sources?: SourceCatalogEntry[]
  onOpenConfig?: (workspace: Workspace) => void
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
  workspace,
  workspaces,
  onPickWorkspace,
  onNewWorkspace,
  autoFocus = false,
  compact = false,
  variant,
  sources = [],
  onOpenConfig,
}: SearchBarProps) {
  const isHero = variant === "hero"
  const [acOpen, setAcOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)

  useEffect(() => {
    if (autoFocus) inputRef.current?.focus()
  }, [autoFocus])

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
    if (!q) return
    setAcOpen(false)
    onSubmit(q)
  }

  const hasContent =
    filtered.length > 0 || (!!value.trim() && filtered.length === 0) || otherWorkspaces.length > 0

  const dropdown = acOpen && hasContent && (
    <div
      className="absolute left-0 right-0 top-full mt-2 z-40 rounded-xl border border-[hsl(var(--border-strong))] bg-[hsl(var(--popover))] shadow-[var(--elev-3)] overflow-hidden"
      onMouseDown={(e) => e.preventDefault()}
    >
      <CommandList>
        {filtered.length === 0 && !!value.trim() && (
          <CommandEmpty className="py-3 px-3 text-sm">
            <span className="inline-flex items-center gap-2">
              <Search className="h-3.5 w-3.5 opacity-60" />
              Search "{value}" in <b>{workspace.name}</b>
            </span>
          </CommandEmpty>
        )}
        {filtered.length > 0 && (
          <CommandGroup heading={`Recent in ${workspace.name}`}>
            {filtered.map((p) => (
              <CommandItem
                key={p.q}
                value={p.q}
                onSelect={() => submit(p.q)}
                className="flex items-center gap-2"
              >
                <Clock className="h-3.5 w-3.5 opacity-60" />
                <span className="flex-1 truncate">{p.q}</span>
                <span className="text-[11px] font-mono text-[hsl(var(--muted-foreground))]">
                  {p.results} · {p.when}
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
                >
                  <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))] mr-2" />
                  <span className="flex-1 truncate">{w.name}</span>
                  <span className="text-[11px] font-mono text-[hsl(var(--muted-foreground))]">
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
      <div ref={wrapRef} className="relative w-full max-w-[720px] mx-auto">
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
              "relative block bg-[hsl(var(--card))] border border-[hsl(var(--border-strong))] rounded-2xl px-3.5 pt-3.5 pb-2.5 min-h-[96px]",
              "shadow-[var(--elev-2)] transition-shadow",
              "focus-within:shadow-[var(--elev-3),_0_0_0_4px_hsl(var(--ring)/0.1)] focus-within:border-[hsl(var(--ring)/0.55)]"
            )}
          >
            <button
              type="button"
              tabIndex={-1}
              aria-label="Expand"
              className="absolute top-2.5 right-3 h-6 w-6 rounded-md inline-flex items-center justify-center text-[hsl(var(--muted-foreground))] opacity-60 hover:opacity-100 hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
            >
              <Maximize2 className="h-3.5 w-3.5" />
            </button>
            <CmdK.Input
              ref={inputRef}
              value={value}
              onValueChange={(v: string) => {
                onChange(v)
                setAcOpen(true)
              }}
              onFocus={() => setAcOpen(true)}
              placeholder="Ask or search the workspace…"
              className="block w-full bg-transparent border-0 outline-none px-1 pb-3 pr-10 text-base text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))]"
            />
            <div className="flex items-center justify-between gap-2">
              <div className="inline-flex items-center gap-1">
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="Attach"
                  className="h-[30px] w-[30px] rounded-md inline-flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
                >
                  <Plus className="h-4 w-4" />
                </button>
                <WorkspacePill
                  workspace={workspace}
                  workspaces={workspaces}
                  onPick={onPickWorkspace}
                  onNew={onNewWorkspace}
                  inline
                />
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
              </div>
              <div className="inline-flex items-center gap-1">
                <button
                  type="button"
                  tabIndex={-1}
                  aria-label="Voice"
                  className="h-[30px] w-[30px] rounded-md inline-flex items-center justify-center text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
                >
                  <Mic className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => submit()}
                  disabled={!value.trim()}
                  aria-label="Search"
                  className={cn(
                    "h-8 w-8 rounded-md inline-flex items-center justify-center transition-colors",
                    value.trim()
                      ? "bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:brightness-110"
                      : "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] cursor-not-allowed"
                  )}
                >
                  <ArrowUp className="h-4 w-4" />
                </button>
              </div>
            </div>
          </div>
          {dropdown}
        </CmdK>
      </div>
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
            "flex items-center gap-1 px-2 rounded-full border bg-[hsl(var(--card))] transition-shadow",
            "border-[hsl(var(--border-strong))] shadow-[var(--elev-1)]",
            "focus-within:shadow-[var(--elev-2),_0_0_0_4px_hsl(var(--ring)/0.1)] focus-within:border-[hsl(var(--ring)/0.55)]",
            compact ? "h-11" : "h-[54px]"
          )}
        >
          <WorkspacePill
            workspace={workspace}
            workspaces={workspaces}
            onPick={onPickWorkspace}
            onNew={onNewWorkspace}
          />
          <span aria-hidden className="h-5 w-px bg-[hsl(var(--border))] mx-1" />
          <CmdK.Input
            ref={inputRef}
            value={value}
            onValueChange={(v: string) => {
              onChange(v)
              setAcOpen(true)
            }}
            onFocus={() => setAcOpen(true)}
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
          <button
            type="button"
            onClick={() => submit()}
            disabled={!value.trim()}
            aria-label="Search"
            className={cn(
              // Smaller than the bar by ~16px so there's a visible inset on
              // top, bottom, AND right (matching the px-2 left/right padding
              // above). Square in compact mode, round in hero so it echoes
              // the pill bar's curve.
              "inline-flex items-center justify-center transition-colors flex-none",
              compact ? "h-7 w-7 rounded-md" : "h-9 w-9 rounded-full",
              value.trim()
                ? "bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:opacity-90"
                : "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] cursor-not-allowed"
            )}
          >
            <ArrowUp className={compact ? "h-3.5 w-3.5" : "h-4 w-4"} />
          </button>
        </div>
        {dropdown}
      </CmdK>
    </div>
  )
}
