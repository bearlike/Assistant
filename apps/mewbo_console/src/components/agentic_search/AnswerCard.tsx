import { ArrowUpRight, Copy, Share, Sparkles, ThumbsUp } from "lucide-react"
import { cn } from "@/lib/utils"

import type { RunAnswer, SearchResult, SourceCatalogEntry } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

interface AnswerCardProps {
  answer: RunAnswer
  results: SearchResult[]
  sources: SourceCatalogEntry[]
  elapsed: number
  totalMs: number
  onCiteClick: (resultId: string) => void
  onAsk: () => void
}

export function AnswerCard({
  answer,
  results,
  sources,
  elapsed,
  totalMs,
  onCiteClick,
  onAsk,
}: AnswerCardProps) {
  const ready = elapsed > totalMs * 0.55
  const visibleBullets = ready
    ? Math.min(answer.bullets.length, Math.floor((elapsed - totalMs * 0.55) / 280))
    : 0

  return (
    <section
      className={cn(
        "relative rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5 shadow-[var(--elev-2)]",
        "before:absolute before:left-0 before:top-3 before:bottom-3 before:w-[3px] before:rounded-r before:bg-[hsl(var(--primary))]",
        !ready && "before:animate-pulse"
      )}
    >
      <header className="flex items-start gap-3 mb-3">
        <div className="flex items-center gap-2 flex-1">
          <span className="inline-flex items-center justify-center h-6 w-6 rounded-md bg-[hsl(var(--primary)/0.15)] text-[hsl(var(--primary))]">
            <Sparkles className="h-3.5 w-3.5" />
          </span>
          <span className="text-sm font-semibold">Synthesis</span>
          <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
            {ready
              ? `${answer.sources_count} sources · ${(elapsed / 1000).toFixed(1)}s`
              : "synthesising…"}
          </span>
        </div>
        <div className="flex items-center gap-1 text-[hsl(var(--muted-foreground))]">
          <IconButton title="Copy answer">
            <Copy className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton title="Share">
            <Share className="h-3.5 w-3.5" />
          </IconButton>
          <IconButton title="Helpful">
            <ThumbsUp className="h-3.5 w-3.5" />
          </IconButton>
        </div>
      </header>

      {!ready ? (
        <div className="space-y-2">
          <SkeletonLine width="90%" />
          <SkeletonLine width="70%" />
          <SkeletonLine width="60%" />
        </div>
      ) : (
        <>
          <p className="text-[15px] leading-relaxed max-w-[64ch] text-[hsl(var(--foreground))] [text-wrap:pretty]">
            {answer.tldr}
          </p>
          <ul className="mt-4 space-y-2.5">
            {answer.bullets.slice(0, visibleBullets).map((b, i) => (
              <li key={i} className="flex items-start gap-2 text-[14px] [text-wrap:pretty]">
                <span className="mt-2 inline-block h-1 w-1 rounded-full bg-[hsl(var(--muted-foreground))] flex-none" />
                <span className="flex-1">
                  {b.text}{" "}
                  {b.cites.map((rid) => {
                    const r = results.find((x) => x.id === rid)
                    if (!r) return null
                    const num = results.findIndex((x) => x.id === rid) + 1
                    const src = sources.find((s) => s.id === r.source)
                    return (
                      <button
                        key={rid}
                        type="button"
                        onClick={() => onCiteClick(rid)}
                        title={r.title}
                        className="inline-flex items-center gap-1 px-1.5 py-0.5 mx-0.5 rounded-full text-xs font-mono bg-[hsl(var(--muted))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--primary))] transition-colors align-middle"
                      >
                        <SrcAvatar source={src} size={12} />
                        <span>{num}</span>
                      </button>
                    )
                  })}
                </span>
              </li>
            ))}
          </ul>

          {visibleBullets >= answer.bullets.length && (
            <footer className="flex items-center gap-3 mt-5 pt-3 border-t border-[hsl(var(--border))]">
              <ConfidenceBar confidence={answer.confidence} />
              <button
                type="button"
                onClick={onAsk}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full text-xs font-medium bg-[hsl(var(--primary)/0.12)] text-[hsl(var(--primary))] hover:bg-[hsl(var(--primary)/0.2)] transition-colors"
              >
                <Sparkles className="h-3 w-3" />
                Ask a follow-up
                <ArrowUpRight className="h-3 w-3" />
              </button>
            </footer>
          )}
        </>
      )}
    </section>
  )
}

function IconButton({ children, title }: { children: React.ReactNode; title: string }) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      className="inline-flex items-center justify-center h-6 w-6 rounded-md hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
    >
      {children}
    </button>
  )
}

function SkeletonLine({ width }: { width: string }) {
  return (
    <div
      className="h-3.5 rounded bg-[hsl(var(--muted))] animate-pulse"
      style={{ width }}
    />
  )
}

// Map a confidence value to a semantic CSS token.
// We don't have a --warning token; --primary is the brand clay (orange-amber)
// which fits the "medium" tier, and --agent-3 is yellow which fits "low".
function confidenceColor(pct: number): string {
  if (pct >= 80) return "hsl(var(--success))"
  if (pct >= 60) return "hsl(var(--primary))"
  if (pct >= 40) return "hsl(var(--agent-3))"
  return "hsl(var(--destructive))"
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100)
  const color = confidenceColor(pct)
  return (
    <div className="flex items-center gap-2 flex-1 text-xs">
      <span className="relative inline-block h-1 w-24 rounded-full bg-[hsl(var(--muted))] overflow-hidden">
        <span
          className="absolute inset-y-0 left-0 transition-[width,background-color] duration-150 ease-out"
          style={{ width: `${pct}%`, background: color }}
        />
      </span>
      <span className="font-mono" style={{ color }}>
        {pct}% confidence
      </span>
    </div>
  )
}
