import {
  ArrowUpRight,
  ChevronRight,
  Layers,
  Sparkles,
  Users,
  Workflow,
} from "lucide-react"
import { cn } from "@/lib/utils"

import type {
  RelatedPerson,
  RunStats,
  SourceCatalogEntry,
  TraceAgent,
} from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"
import {
  agentSnapshot,
  compactTokens,
  humanizeMs,
  laneSource,
  runProgress,
} from "./utils"

interface RightRailProps {
  agents: TraceAgent[]
  sources: SourceCatalogEntry[]
  /** Run-level instrument totals (`payload.stats`) — present on snapshots. */
  stats?: RunStats | null
  related: string[]
  people: RelatedPerson[]
  /** Run reached a terminal state. */
  done: boolean
  traceActive: boolean
  onShowTrace: () => void
  onAsk: (query: string) => void
  /** Open the workspace's capability graph (#79). */
  onShowGraph?: () => void
}

export function RightRail({
  agents,
  sources,
  stats,
  related,
  people,
  done,
  traceActive,
  onShowTrace,
  onAsk,
  onShowGraph,
}: RightRailProps) {
  const progress = runProgress(agents, done)

  return (
    <aside className="hidden min-[1100px]:flex flex-col gap-3 w-[340px] flex-none sticky top-4 self-start">
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
        <ul className="px-3 pb-3 space-y-1">
          {agents.slice(0, 8).map((a) => (
            <LaneRow key={a.id} agent={a} sources={sources} />
          ))}
        </ul>
        <div className="h-0.5 w-full bg-[hsl(var(--muted))] relative">
          <span
            className="absolute inset-y-0 left-0 bg-[hsl(var(--primary))] transition-[width]"
            style={{ width: `${progress * 100}%` }}
          />
        </div>
        {/* Overall run-stats block — renders only when the BE stamped stats, and
            only the present/non-null fields within (honesty rule). */}
        <RunStatsBlock stats={stats} />
        {onShowGraph && (
          <button
            type="button"
            onClick={onShowGraph}
            className="w-full flex items-center gap-2 px-4 py-2.5 border-t border-[hsl(var(--border))] hover:bg-[hsl(var(--accent))] transition-colors text-left"
          >
            <Workflow className="h-3.5 w-3.5 text-[hsl(var(--primary))]" />
            <span className="text-sm font-medium flex-1">Capability graph</span>
            <ChevronRight className="h-3 w-3 opacity-60" />
          </button>
        )}
      </div>

      {/* Hide-when-empty: related questions arrive on the dedicated
          `related_questions` event (a parallel structured call at settle). */}
      {related.length > 0 && (
        <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 shadow-[var(--elev-2)] hover:shadow-[var(--elev-3)] hover:border-[hsl(var(--border-strong))] transition-shadow">
          <div className="flex items-center gap-2 mb-1.5">
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
      )}

      {people.length > 0 && (
        <div className="rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 shadow-[var(--elev-2)] hover:shadow-[var(--elev-3)] hover:border-[hsl(var(--border-strong))] transition-shadow">
          <div className="flex items-center gap-2 mb-1.5">
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
      )}
    </aside>
  )
}

/**
 * One per-lane instrument row: brand/coordinator glyph + kind name (line 1) and
 * a metric strip (model · steps · duration · tokens · results — each rendered
 * ONLY when present). The lane `name` IS the kind (BE contract); the model
 * arrives separately on `agent.model`.
 */
function LaneRow({
  agent,
  sources,
}: {
  agent: TraceAgent
  sources: SourceCatalogEntry[]
}) {
  const { done, running } = agentSnapshot(agent)
  const { source: src, isCoordinator } = laneSource(agent, sources)
  // The lane's role: explicit `kind` if the BE sent one, else the name (which
  // becomes the kind on the wire), else the catalog source name.
  const kindLabel = agent.kind ?? agent.name ?? src?.name ?? "lane"
  const duration = humanizeMs(agent.duration_ms)
  const tokensIn = compactTokens(agent.input_tokens)
  const tokensOut = compactTokens(agent.output_tokens)
  const tokens =
    tokensIn || tokensOut ? `${tokensIn || "0"}→${tokensOut || "0"} tok` : ""
  // The lane's result contribution: KEPT (its own count) + how many it emitted
  // that were FILTERED as duplicates (returned − kept). The kept count rides a
  // prominent pip on line 1 (the headline "how much did this tool contribute");
  // the filtered delta goes in the metric strip so the dedup work is visible.
  const kept = !isCoordinator ? agent.results_count : null
  const filtered =
    agent.returned_count != null && kept != null && agent.returned_count > kept
      ? agent.returned_count - kept
      : 0
  // Metric strip parts — only the present ones (never a fabricated 0).
  const metrics: string[] = []
  if (agent.model) metrics.push(agent.model)
  if (agent.steps != null) metrics.push(`${agent.steps} step${agent.steps === 1 ? "" : "s"}`)
  if (duration) metrics.push(duration)
  if (tokens) metrics.push(tokens)
  if (filtered > 0) metrics.push(`${filtered} filtered`)

  return (
    <li className="flex items-start gap-2 px-2 py-1.5 rounded">
      {isCoordinator ? (
        <Workflow
          className={cn(
            "h-3.5 w-3.5 flex-none mt-0.5 opacity-70",
            running && "animate-pulse"
          )}
        />
      ) : (
        <span className="mt-0.5">
          <SrcAvatar source={src} size={14} />
        </span>
      )}
      <div className="flex-1 min-w-0">
        <div
          className={cn(
            "flex items-center gap-1.5 text-xs",
            done
              ? "text-[hsl(var(--foreground))]"
              : running
              ? "text-[hsl(var(--primary))]"
              : "text-[hsl(var(--muted-foreground))]"
          )}
        >
          <span className="flex-1 truncate font-medium">{kindLabel}</span>
          {kept != null && (
            <ResultCountPip
              kept={kept}
              filtered={filtered}
              dim={!done && !running}
            />
          )}
          <span className="font-mono text-[11px] flex-none">
            {done ? "✓" : running ? "…" : "·"}
          </span>
        </div>
        {metrics.length > 0 && (
          <div className="mt-0.5 text-[10.5px] text-[hsl(var(--muted-foreground))] font-mono tabular-nums truncate">
            {metrics.join(" · ")}
          </div>
        )}
      </div>
    </li>
  )
}

/**
 * Per-lane result-count pip — the headline "how many results this tool
 * contributed". The right-surface count-pip vocabulary (`rounded`, mono); the
 * tooltip spells out kept vs. filtered so the dedup work is legible.
 */
function ResultCountPip({
  kept,
  filtered,
  dim,
}: {
  kept: number
  filtered: number
  dim: boolean
}) {
  const title =
    filtered > 0
      ? `${kept} result${kept === 1 ? "" : "s"} · ${filtered} filtered as duplicate${filtered === 1 ? "" : "s"}`
      : `${kept} result${kept === 1 ? "" : "s"}`
  return (
    <span
      title={title}
      className={cn(
        "flex-none inline-flex items-center justify-center min-w-[1.25rem] px-1 h-4 rounded font-mono tabular-nums text-[10px] font-medium",
        kept > 0
          ? "bg-[hsl(var(--primary)/0.14)] text-[hsl(var(--primary))]"
          : "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]",
        dim && "opacity-60"
      )}
    >
      {kept}
    </span>
  )
}

/**
 * Run-level instrument totals (`payload.stats`). Renders nothing when stats is
 * absent, and within the block renders only present/non-null fields — the
 * setup/search split appears only when at least one side is known. Never
 * fabricates a 0 for an unknown total.
 */
function RunStatsBlock({ stats }: { stats?: RunStats | null }) {
  if (!stats) return null
  const tokens =
    stats.input_tokens > 0 || stats.output_tokens > 0
      ? `${compactTokens(stats.input_tokens)}→${compactTokens(stats.output_tokens)} tok`
      : ""
  const phaseParts: string[] = []
  if (stats.setup_ms != null) phaseParts.push(`setup ${humanizeMs(stats.setup_ms)}`)
  if (stats.search_ms != null) phaseParts.push(`search ${humanizeMs(stats.search_ms)}`)

  // Top row: probes + tool calls (each only when > 0).
  const headParts: string[] = []
  if (stats.probes > 0) headParts.push(`${stats.probes} probe${stats.probes === 1 ? "" : "s"}`)
  if (stats.tool_calls > 0)
    headParts.push(`${stats.tool_calls} tool call${stats.tool_calls === 1 ? "" : "s"}`)

  if (headParts.length === 0 && !tokens && phaseParts.length === 0) return null

  return (
    <div className="px-4 py-2.5 border-t border-[hsl(var(--border))] space-y-1 text-[11px] text-[hsl(var(--muted-foreground))] font-mono tabular-nums">
      {headParts.length > 0 && <div>{headParts.join(" · ")}</div>}
      {tokens && <div>{tokens}</div>}
      {phaseParts.length > 0 && <div>{phaseParts.join(" · ")}</div>}
    </div>
  )
}
