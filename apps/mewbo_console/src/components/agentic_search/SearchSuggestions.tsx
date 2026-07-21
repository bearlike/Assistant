import { Clock, Search } from "lucide-react"

import {
  CommandGroup,
  CommandItem,
  CommandList,
  CommandSeparator,
} from "@/components/ui/command"
import { RelativeTime } from "../../utils/relativeTime"
import type { PastQuery, Workspace } from "../../types/agenticSearch"
import { pastQueryKey } from "./utils"

interface SearchSuggestionsProps {
  acOpen: boolean
  filtered: PastQuery[]
  otherWorkspaces: Workspace[]
  value: string
  workspace: Workspace
  /** Submit the composer with this text. Callers close the dropdown before
   *  invoking (mirrors the root `submit()` closure this was extracted from). */
  onSubmit: (value: string) => void
  /** Replay a stored run by id. Callers close the dropdown before invoking. */
  onReplay?: (runId: string) => void
  /** Switch workspace. Callers close the dropdown before invoking. */
  onSelectWorkspace: (workspace: Workspace) => void
}

/**
 * Suggestions dropdown — a search-suggest-style extension of the composer
 * surface. It anchors tight to the bar (mt-1, same border-strong + radius
 * family + elev-3) and pans out from beneath it via the `.composer-suggest`
 * origin-top entrance (index.css, reduced-motion safe). One typographic
 * scale: group headings (cmdk `text-xs` muted), item label (text-sm), and a
 * consistent right meta column that keeps `font-mono` ONLY on data
 * (counts/times). Icons are uniform 14px (`[&_svg]:size-3.5`) at one opacity.
 */
export function SearchSuggestions({
  acOpen,
  filtered,
  otherWorkspaces,
  value,
  workspace,
  onSubmit,
  onReplay,
  onSelectWorkspace,
}: SearchSuggestionsProps) {
  const hasContent =
    filtered.length > 0 || (!!value.trim() && filtered.length === 0) || otherWorkspaces.length > 0
  if (!acOpen || !hasContent) return null

  return (
    <div
      className="composer-suggest absolute left-0 right-0 top-full mt-1 z-40 rounded-xl border border-[hsl(var(--border-strong))] bg-[hsl(var(--popover))] [box-shadow:var(--elev-3)] overflow-hidden"
      onMouseDown={(e) => e.preventDefault()}
    >
      <CommandList className="[&_[cmdk-item]_svg]:size-3.5">
        {filtered.length === 0 && !!value.trim() && (
          // Empty "search this" row — same icon size/opacity + gap rhythm as
          // the item rows so it reads as a peer, not a one-off.
          <CommandGroup heading={workspace.name}>
            <CommandItem
              value={`__search__${value}`}
              onSelect={() => onSubmit(value)}
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
                    onReplay(p.run_id)
                  } else {
                    onSubmit(p.q)
                  }
                }}
                className="gap-2.5"
              >
                <Clock className="opacity-60" />
                <span className="flex-1 truncate">{p.q}</span>
                <span className="flex-none pl-3 text-2xs font-mono tabular-nums text-[hsl(var(--muted-foreground))]">
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
                  onSelect={() => onSelectWorkspace(w)}
                  className="gap-2.5"
                >
                  {/* Workspace dot occupies the same 14px icon slot as the
                      Clock/Search glyphs so the gap rhythm stays uniform. */}
                  <span className="flex h-3.5 w-3.5 items-center justify-center">
                    <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))]" />
                  </span>
                  <span className="flex-1 truncate">{w.name}</span>
                  <span className="flex-none pl-3 text-2xs font-mono tabular-nums text-[hsl(var(--muted-foreground))]">
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
}
