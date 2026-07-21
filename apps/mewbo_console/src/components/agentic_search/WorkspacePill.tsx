import { useState } from "react"
import { Check, ChevronDown, Plus } from "lucide-react"

import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { cn } from "@/lib/utils"
import type { Workspace } from "../../types/agenticSearch"

/**
 * Workspace selector. Two visual modes — chip (default) for the compact
 * results-topbar bar, and inline (transparent) for the hero footer where
 * the bar's own border is the container.
 */
interface WorkspacePillProps {
  workspace: Workspace
  workspaces: Workspace[]
  onSelect: (workspace: Workspace) => void
  onNew: () => void
  inline?: boolean
}

export function WorkspacePill({ workspace, workspaces, onSelect, onNew, inline }: WorkspacePillProps) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          className={cn(
            "inline-flex items-center gap-2 transition-colors flex-none rounded-md font-medium",
            inline
              ? "h-[30px] px-2.5 text-sm text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]"
              : "h-8 px-2.5 text-sm hover:bg-[hsl(var(--accent))]"
          )}
        >
          <span className="rounded-full bg-[hsl(var(--primary))] h-1.5 w-1.5 flex-none" />
          <span className="truncate max-w-[72px] sm:max-w-[140px]">{workspace.name}</span>
          <ChevronDown className="h-3 w-3 opacity-60" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72 p-1 [box-shadow:var(--elev-3)]">
        <div className="px-2 py-1.5 text-2xs uppercase tracking-wider text-[hsl(var(--muted-foreground))]">
          Your workspaces
        </div>
        <ul className="space-y-0.5">
          {workspaces.map((w) => (
            <li key={w.id}>
              <button
                type="button"
                onClick={() => {
                  onSelect(w)
                  setOpen(false)
                }}
                className={cn(
                  "w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm hover:bg-[hsl(var(--accent))] transition-colors",
                  w.id === workspace.id && "bg-[hsl(var(--accent))]"
                )}
              >
                <span className="flex-1 truncate">{w.name}</span>
                <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                  {w.sources.length} sources
                </span>
                {w.id === workspace.id && (
                  <Check className="h-3.5 w-3.5 text-[hsl(var(--primary-text))]" />
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
            className="w-full flex items-center gap-2 px-2 py-1.5 rounded-md text-left text-sm text-[hsl(var(--primary-text))] hover:bg-[hsl(var(--accent))]"
          >
            <Plus className="h-3.5 w-3.5" />
            New workspace
          </button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
