import { ChevronRight, Layers, Workflow } from "lucide-react"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet"
import { cn } from "@/lib/utils"

import type { TraceAgent } from "../../types/agenticSearch"
import { agentSnapshot, compactTokens, humanizeMs, runProgress } from "./utils"

interface TraceDrawerProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agents: TraceAgent[]
  query: string
  /** Real elapsed ms since run start — display only. */
  elapsedMs: number
  done: boolean
}

export function TraceDrawer({
  open,
  onOpenChange,
  agents,
  query,
  elapsedMs,
  done,
}: TraceDrawerProps) {
  const progress = runProgress(agents, done)
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
            {agents.length} {agents.length === 1 ? "lane" : "lanes"}
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
              spawned {agents.length} {agents.length === 1 ? "lane" : "lanes"}
            </div>
          </div>
          {agents.map((agent) => (
            <AgentBlock key={agent.id} agent={agent} />
          ))}
        </div>
        <footer className="flex items-center justify-between px-5 py-3 border-t border-[hsl(var(--border))] text-xs text-[hsl(var(--muted-foreground))] font-mono">
          <span>elapsed {(elapsedMs / 1000).toFixed(1)}s</span>
          <span>{done ? "aggregated · ranked" : "streaming…"}</span>
        </footer>
      </SheetContent>
    </Sheet>
  )
}

function AgentBlock({ agent }: { agent: TraceAgent }) {
  const { visibleLines: visible, done, running } = agentSnapshot(agent)
  const slotColor = `hsl(var(--agent-${agent.slot}))`
  // A dead-ended lane (the BE's `agent_done.empty`, surfaced on the terminal
  // line) reads as signal, not failure noise — the NO-DATA verdict is the point.
  const deadEnd = agent.lines.some((l) => l.empty)
  // The coordinator lane (root agent's tool activity) has no catalog source —
  // mark it with a glyph so it reads as "the orchestrator", not a probe.
  const isCoordinator = agent.source_id === ""
  return (
    <div
      className="border-l-2 pl-3"
      style={{ borderColor: slotColor }}
    >
      <div className="flex items-center gap-2 mb-1">
        {isCoordinator ? (
          <Workflow
            className={cn(
              "h-3 w-3 flex-none text-[hsl(var(--code-fg-muted))]",
              running && "animate-pulse"
            )}
          />
        ) : (
          <span
            className={cn("h-1.5 w-1.5 rounded-full", running && "animate-pulse")}
            style={{ background: slotColor }}
          />
        )}
        {/* The lane's kind (its role) leads; the model is a separate field. */}
        <span className="font-medium text-[hsl(var(--foreground))]">
          {agent.kind ?? agent.name}
        </span>
        {agent.model && (
          <span className="text-[11px] text-[hsl(var(--code-fg-muted))]">{agent.model}</span>
        )}
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
      {/* Per-lane instrument strip — only the present metrics (honesty rule). */}
      {(() => {
        const m: string[] = []
        if (agent.steps != null) m.push(`${agent.steps} step${agent.steps === 1 ? "" : "s"}`)
        const dur = humanizeMs(agent.duration_ms)
        if (dur) m.push(dur)
        const ti = compactTokens(agent.input_tokens)
        const to = compactTokens(agent.output_tokens)
        if (ti || to) m.push(`${ti || "0"}→${to || "0"} tok`)
        if (agent.results_count != null && !isCoordinator) {
          m.push(`${agent.results_count} result${agent.results_count === 1 ? "" : "s"}`)
          // How many this lane emitted that collapsed into another lane's card.
          const filtered =
            agent.returned_count != null && agent.returned_count > agent.results_count
              ? agent.returned_count - agent.results_count
              : 0
          if (filtered > 0) m.push(`${filtered} filtered`)
        }
        return m.length > 0 ? (
          <div className="mb-1 text-[11px] text-[hsl(var(--code-fg-subtle))] tabular-nums">
            {m.join(" · ")}
          </div>
        ) : null
      })()}
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
      {agent.result && (
        // The probe's actual response — its `EVIDENCE (pathway: …)` / `NO DATA`
        // block. Native `<details>` (KISS — no vendored Collapsible), collapsed
        // by default so the race view stays scannable. Dead-ends rail in primary.
        <details className="group/ev mt-1.5">
          <summary className="flex items-center gap-1 cursor-pointer select-none list-none text-[11px] text-[hsl(var(--code-fg-muted))] hover:text-[hsl(var(--code-fg))]">
            <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open/ev:rotate-90" />
            {deadEnd ? "no data — pathway dead-ended" : "evidence returned"}
          </summary>
          <pre
            className="mt-1 whitespace-pre-wrap break-words border-l-2 pl-2.5 py-1 text-[11.5px] leading-[1.5] text-[hsl(var(--code-fg))]"
            style={{ borderColor: deadEnd ? "hsl(var(--primary))" : slotColor }}
          >
            {agent.result}
          </pre>
        </details>
      )}
    </div>
  )
}
