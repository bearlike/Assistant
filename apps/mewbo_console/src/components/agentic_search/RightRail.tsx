import { ArrowUpRight, ChevronRight, Layers, Sparkles, Users } from "lucide-react"
import { cn } from "@/lib/utils"

import type {
  RelatedPerson,
  SourceCatalogEntry,
  TraceAgent,
} from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"
import { agentSnapshot } from "./utils"

interface RightRailProps {
  agents: TraceAgent[]
  sources: SourceCatalogEntry[]
  related: string[]
  people: RelatedPerson[]
  elapsed: number
  totalMs: number
  traceActive: boolean
  onShowTrace: () => void
  onAsk: (query: string) => void
}

export function RightRail({
  agents,
  sources,
  related,
  people,
  elapsed,
  totalMs,
  traceActive,
  onShowTrace,
  onAsk,
}: RightRailProps) {
  const progress = Math.min(1, totalMs > 0 ? elapsed / totalMs : 0)

  return (
    <aside className="hidden min-[1100px]:flex flex-col gap-4 w-[270px] flex-none sticky top-4 self-start">
      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden shadow-[var(--elev-2)] hover:shadow-[var(--elev-3)] hover:border-[hsl(var(--border-strong))] transition-shadow">
        <button
          type="button"
          onClick={onShowTrace}
          className="w-full flex items-center gap-2 px-4 py-3 hover:bg-[hsl(var(--accent))] transition-colors text-left"
        >
          <Layers className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
          <span className="text-sm font-medium flex-1">Agent trace</span>
          {traceActive && (
            <span className="h-1.5 w-1.5 rounded-full bg-[hsl(var(--primary))] animate-pulse" />
          )}
          <ChevronRight className="h-3 w-3 opacity-60" />
        </button>
        <ul className="px-3 pb-3 space-y-0.5">
          {agents.slice(0, 8).map((a) => {
            const { done, running } = agentSnapshot(a, elapsed)
            const src = sources.find((s) => s.id === a.source_id)
            return (
              <li
                key={a.id}
                className={cn(
                  "flex items-center gap-2 px-2 py-1 rounded text-xs",
                  done
                    ? "text-[hsl(var(--foreground))]"
                    : running
                    ? "text-[hsl(var(--primary))]"
                    : "text-[hsl(var(--muted-foreground))]"
                )}
              >
                <SrcAvatar source={src} size={14} />
                <span className="flex-1 truncate">{src?.name ?? a.name}</span>
                <span className="font-mono text-[11px]">
                  {done ? "✓" : running ? "…" : "·"}
                </span>
              </li>
            )
          })}
        </ul>
        <div className="h-0.5 w-full bg-[hsl(var(--muted))] relative">
          <span
            className="absolute inset-y-0 left-0 bg-[hsl(var(--primary))] transition-[width]"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </div>

      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 shadow-[var(--elev-2)] hover:shadow-[var(--elev-3)] hover:border-[hsl(var(--border-strong))] transition-shadow">
        <div className="flex items-center gap-2 mb-2">
          <Sparkles className="h-3.5 w-3.5" />
          <span className="text-sm font-medium">Related questions</span>
        </div>
        <ul className="space-y-0.5">
          {related.map((q, i) => (
            <li key={i}>
              <button
                type="button"
                onClick={() => onAsk(q)}
                className="w-full flex items-center gap-2 px-2 py-1.5 rounded text-left text-[13px] hover:bg-[hsl(var(--accent))] transition-colors group"
              >
                <span className="flex-1">{q}</span>
                <ArrowUpRight className="h-3 w-3 opacity-40 group-hover:opacity-100 group-hover:text-[hsl(var(--primary))] transition-opacity flex-none" />
              </button>
            </li>
          ))}
        </ul>
      </div>

      <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 shadow-[var(--elev-2)] hover:shadow-[var(--elev-3)] hover:border-[hsl(var(--border-strong))] transition-shadow">
        <div className="flex items-center gap-2 mb-2">
          <Users className="h-3.5 w-3.5" />
          <span className="text-sm font-medium">People</span>
        </div>
        <ul className="space-y-1.5">
          {people.map((p, i) => (
            <li key={i} className="flex items-center gap-2 text-xs">
              <span
                className="inline-flex items-center justify-center h-7 w-7 rounded-full font-mono font-semibold text-[10px]"
                style={{
                  background: `hsl(var(--agent-${p.color}) / 0.18)`,
                  color: `hsl(var(--agent-${p.color}))`,
                }}
              >
                {p.initials}
              </span>
              <div className="flex-1 min-w-0">
                <div className="text-[13px] font-medium truncate">{p.name}</div>
                <div className="text-[11px] text-[hsl(var(--muted-foreground))] truncate">
                  {p.role}
                </div>
              </div>
            </li>
          ))}
        </ul>
      </div>
    </aside>
  )
}
