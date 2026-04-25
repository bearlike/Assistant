import { Layers } from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

import type { TraceAgent } from "../../types/agenticSearch"
import { agentSnapshot } from "./utils"

interface TraceDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agents: TraceAgent[]
  query: string
  elapsed: number
  totalMs: number
  done: boolean
}

export function TraceDrawer({
  open,
  onOpenChange,
  agents,
  query,
  elapsed,
  totalMs,
  done,
}: TraceDrawerProps) {
  const progress = Math.min(1, totalMs > 0 ? elapsed / totalMs : 0)
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-md p-0 flex flex-col bg-[hsl(var(--background))] shadow-[var(--elev-3)]"
      >
        <SheetHeader className="px-5 pt-5 pb-3 border-b border-[hsl(var(--border))]">
          <SheetTitle className="flex items-center gap-2 text-base">
            <Layers className="h-4 w-4 text-[hsl(var(--primary))]" />
            Agent trace
          </SheetTitle>
          <SheetDescription className="font-mono text-xs">
            {agents.length} sub-agents · budget 8
          </SheetDescription>
        </SheetHeader>
        <div className="h-0.5 w-full bg-[hsl(var(--muted))] relative">
          <span
            className="absolute inset-y-0 left-0 bg-[hsl(var(--primary))] transition-[width]"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-4 space-y-5 font-mono text-[12.5px] text-[hsl(var(--code-fg))] bg-[hsl(var(--code-body))]">
          <div className="text-[hsl(var(--code-fg-muted))] leading-relaxed">
            <span className="text-[hsl(var(--code-prompt))]">▶</span>{" "}
            <span className="text-[hsl(var(--code-fg))]">plan_search</span>(
            <span className="text-[hsl(var(--hl-string))]">"{query}"</span>)
            <div className="mt-1 text-[11px] text-[hsl(var(--code-fg-subtle))]">
              spawned {agents.length} sub-agents · budget 8
            </div>
          </div>
          {agents.map((agent) => (
            <AgentBlock key={agent.id} agent={agent} elapsed={elapsed} />
          ))}
        </div>
        <footer className="flex items-center justify-between px-5 py-3 border-t border-[hsl(var(--border))] text-xs text-[hsl(var(--muted-foreground))] font-mono">
          <span>elapsed {(elapsed / 1000).toFixed(1)}s</span>
          <span>{done ? "aggregated · ranked" : "streaming…"}</span>
        </footer>
      </SheetContent>
    </Sheet>
  )
}

function AgentBlock({ agent, elapsed }: { agent: TraceAgent; elapsed: number }) {
  const { visibleLines: visible, done, running } = agentSnapshot(agent, elapsed)
  const slotColor = `hsl(var(--agent-${agent.slot}))`
  return (
    <div
      className="border-l-2 pl-3"
      style={{ borderColor: slotColor }}
    >
      <div className="flex items-center gap-2 mb-1">
        <span
          className={cn("h-1.5 w-1.5 rounded-full", running && "animate-pulse")}
          style={{ background: slotColor }}
        />
        <span className="font-medium text-[hsl(var(--foreground))]">{agent.name}</span>
        <span className="text-[11px] text-[hsl(var(--code-fg-muted))]">{agent.agent_id}</span>
        <span
          className={cn(
            "ml-auto text-[11px]",
            done
              ? "text-[hsl(var(--success))]"
              : running
              ? "text-[hsl(var(--primary))]"
              : "text-[hsl(var(--code-fg-subtle))]"
          )}
        >
          {done ? "✓" : running ? `${visible.length}/${agent.lines.length}` : "·"}
        </span>
      </div>
      <div className="space-y-0.5">
        {visible.map((l, i) => {
          const isLast = i === visible.length - 1 && !done
          return (
            <div key={i} className="flex items-start gap-2">
              <span
                className="w-3 flex-none"
                style={
                  l.empty
                    ? { color: "hsl(var(--primary))" }
                    : undefined
                }
              >
                {l.glyph}
              </span>
              <span
                className={cn(
                  "flex-1",
                  l.empty
                    ? "text-[hsl(var(--primary))]"
                    : "text-[hsl(var(--code-fg))]"
                )}
              >
                {l.text}
                {isLast && (
                  <span className="ml-1 inline-block animate-pulse text-[hsl(var(--primary))]">▌</span>
                )}
              </span>
            </div>
          )
        })}
        {visible.length === 0 && (
          <div className="flex items-start gap-2 text-[hsl(var(--code-fg-subtle))]">
            <span className="w-3 flex-none">·</span>
            <span>queued</span>
          </div>
        )}
      </div>
    </div>
  )
}
