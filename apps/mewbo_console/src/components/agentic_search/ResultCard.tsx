import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileText,
  Link as LinkIcon,
  PlayCircle,
  Sparkles,
} from "lucide-react"
import { cn } from "@/lib/utils"

import type { SearchResult, SourceCatalogEntry } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

interface ResultCardProps {
  result: SearchResult
  num: number
  expanded: boolean
  highlighted: boolean
  sources: SourceCatalogEntry[]
  onToggle: () => void
}

function relevanceBucket(rel: number): "high" | "med" | "low" {
  const pct = rel * 100
  if (pct >= 85) return "high"
  if (pct >= 65) return "med"
  return "low"
}

const REL_DOT_COLORS: Record<"high" | "med" | "low", string> = {
  high: "hsl(var(--success))",
  med: "hsl(var(--primary))",
  low: "hsl(var(--muted-foreground))",
}

export function ResultCard({
  result,
  num,
  expanded,
  highlighted,
  sources,
  onToggle,
}: ResultCardProps) {
  const src = sources.find((s) => s.id === result.source)
  const rel = relevanceBucket(result.relevance)
  // Stop click propagation on inner controls so they don't also toggle the card.
  const stop = (fn?: () => void) => (e: React.MouseEvent) => {
    e.stopPropagation()
    fn?.()
  }

  // The result index lives INSIDE the card's padding (top-left, before the
  // source avatar) so every card shares the same left/right edges as the
  // synthesis card above it — no external gutter eating into the column.
  return (
    <article
      id={`result-${result.id}`}
      onClick={onToggle}
      className={cn(
        "group cursor-pointer rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4",
        "shadow-[var(--elev-1)] transition-all duration-150 ease-out",
        "hover:bg-[hsl(var(--accent)/0.45)] hover:border-[hsl(var(--border-strong))] hover:shadow-[var(--elev-2)] hover:-translate-y-px",
        "active:translate-y-0",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))] focus-visible:ring-offset-1 focus-visible:ring-offset-[hsl(var(--background))]",
        highlighted && "ring-2 ring-[hsl(var(--primary))] ring-inset bg-[hsl(var(--primary)/0.06)]"
      )}
    >
      <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))] min-w-0">
        <span className="font-mono tabular-nums w-4 text-right">{num}</span>
        <SrcAvatar source={src} size={16} />
        <span className="font-medium text-[hsl(var(--foreground))]">{src?.name}</span>
        <span className="opacity-50">·</span>
        <span className="truncate" title={result.url}>{result.url}</span>
        <span className="ml-auto flex items-center gap-2 flex-none">
          <span className="font-mono">{result.timestamp}</span>
          <span
            className="inline-block h-1.5 w-1.5 rounded-full"
            title={`Relevance ${Math.round(result.relevance * 100)}%`}
            style={{ background: REL_DOT_COLORS[rel] }}
          />
        </span>
      </div>
      <h3 className="mt-1 text-base font-medium text-[hsl(var(--foreground))] hover:text-[hsl(var(--primary))] transition-colors cursor-pointer">
        {result.title}
      </h3>
      <p
        className="mt-1 text-[13.5px] leading-relaxed text-[hsl(var(--muted-foreground))] [&_mark]:bg-[hsl(var(--primary)/0.18)] [&_mark]:text-[hsl(var(--primary))] [&_mark]:rounded-sm [&_mark]:px-0.5 [&_code]:font-mono [&_code]:text-[12.5px] [&_code]:bg-[hsl(var(--muted))] [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded"
        // Snippet content is server-controlled mock data with <mark> and
        // <code> tags. When real search lands and results may include
        // user-influenced content, switch to structured tokens or
        // sanitized rendering.
        dangerouslySetInnerHTML={{ __html: result.snippet }}
      />

      {expanded && result.image && (
        <div className="mt-3">
          <div
            className="aspect-[16/9] rounded-md"
            style={{ background: result.image.gradient }}
            aria-label={result.image.alt}
          />
        </div>
      )}

      {expanded && result.embed && (
        <div className="mt-3 rounded-md border border-[hsl(var(--code-border))] overflow-hidden bg-[hsl(var(--code-body))]">
          <div className="flex items-center gap-2 px-3 py-1.5 bg-[hsl(var(--code-chrome))] text-xs text-[hsl(var(--code-fg-muted))]">
            <span className="flex items-center gap-1">
              {[0, 1, 2].map((i) => (
                <span key={i} className="h-2 w-2 rounded-full bg-[hsl(var(--muted-foreground)/0.4)]" />
              ))}
            </span>
            <span>{result.embed.kind === "figma" ? "figma.com" : "docs.google.com · slides"}</span>
            <span className="ml-auto text-[hsl(var(--code-fg))]">{result.embed.title}</span>
          </div>
          <div className="aspect-[16/9] flex items-center justify-center text-[hsl(var(--code-fg))] text-sm">
            {result.embed.kind === "figma" ? (
              <div
                className="w-full h-full"
                style={{
                  background:
                    "linear-gradient(135deg, hsl(var(--agent-7) / 0.25), hsl(var(--agent-1) / 0.2))",
                }}
              />
            ) : (
              <div className="flex flex-col items-center gap-2">
                <PlayCircle className="h-6 w-6 opacity-70" />
                <span>{result.embed.title}</span>
              </div>
            )}
          </div>
        </div>
      )}

      {expanded && result.refs && result.refs.length > 0 && (
        <div className="mt-3 border-l-2 border-[hsl(var(--border-strong))] pl-3 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider font-mono text-[hsl(var(--muted-foreground))]">
            Referenced ({result.refs.length})
          </div>
          {result.refs.map((ref) => (
            <div key={ref.url} className="flex items-center gap-2 text-xs">
              <FileText className="h-3 w-3 opacity-60 flex-none" />
              <span className="font-medium truncate">{ref.title}</span>
              <span className="font-mono text-[hsl(var(--muted-foreground))] truncate">
                {ref.url}
              </span>
            </div>
          ))}
        </div>
      )}

      {expanded && result.insight && (
        <div className="mt-3 flex gap-2 p-3 rounded-md bg-[hsl(var(--permission)/0.08)] border border-[hsl(var(--permission)/0.25)]">
          <Sparkles className="h-4 w-4 text-[hsl(var(--permission))] flex-none mt-0.5" />
          <div className="text-[13px]">
            <div className="text-[11px] uppercase tracking-wider font-mono text-[hsl(var(--muted-foreground))] mb-0.5">
              {result.insight.label}
            </div>
            <div className="leading-relaxed">{result.insight.body}</div>
          </div>
        </div>
      )}

      <footer className="flex items-center gap-2 mt-2 text-xs text-[hsl(var(--muted-foreground))]">
        <span className="truncate">{result.author}</span>
        <button
          type="button"
          onClick={stop(onToggle)}
          className="ml-auto inline-flex items-center gap-1 px-2 h-6 rounded hover:bg-[hsl(var(--accent))] transition-colors"
        >
          {expanded ? "Less" : "More"}
          {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
        </button>
        <span className="inline-flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <IconLink title="Open" href={result.url} icon={<ExternalLink className="h-3 w-3" />} />
          <IconLink title="Copy link" icon={<LinkIcon className="h-3 w-3" />} />
          <IconLink title="Ask follow-up" icon={<Sparkles className="h-3 w-3" />} />
        </span>
      </footer>
    </article>
  )
}

function IconLink({
  title,
  icon,
  href,
}: {
  title: string
  icon: React.ReactNode
  href?: string
}) {
  const cls =
    "inline-flex items-center justify-center h-6 w-6 rounded hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors"
  const stopClick = (e: React.MouseEvent) => e.stopPropagation()
  if (href) {
    return (
      <a
        href={href.startsWith("http") ? href : `https://${href}`}
        target="_blank"
        rel="noreferrer"
        onClick={stopClick}
        title={title}
        aria-label={title}
        className={cls}
      >
        {icon}
      </a>
    )
  }
  return (
    <button type="button" onClick={stopClick} title={title} aria-label={title} className={cls}>
      {icon}
    </button>
  )
}
