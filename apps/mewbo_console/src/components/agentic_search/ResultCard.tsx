import {
  ChevronDown,
  ChevronUp,
  ExternalLink,
  FileText,
  Sparkles,
} from "lucide-react"
import { cn } from "@/lib/utils"

import { CopyButton } from "../CopyButton"
import type { SearchResult, SourceCatalogEntry } from "../../types/agenticSearch"
import { metaChips, type MetaChip, type StatusTone } from "./resultMeta"
import { SrcAvatar } from "./SrcAvatar"

interface ResultCardProps {
  result: SearchResult
  num: number
  expanded: boolean
  highlighted: boolean
  sources: SourceCatalogEntry[]
  onToggle: () => void
  /** Prefill the composer with a follow-up about this result + focus it. */
  onAskFollowUp?: (result: SearchResult) => void
}

/** Kind → short label for the card's kind badge. */
const KIND_LABEL: Record<SearchResult["kind"], string> = {
  docs: "Docs",
  code: "Code",
  threads: "Thread",
  design: "Design",
  tickets: "Ticket",
  web: "Web",
}

function relevanceBucket(rel: number): "high" | "med" | "low" {
  const pct = rel * 100
  if (pct >= 85) return "high"
  if (pct >= 65) return "med"
  return "low"
}

const REL_LABEL: Record<"high" | "med" | "low", string> = {
  high: "High relevance",
  med: "Medium relevance",
  low: "Low relevance",
}
const REL_DOT_COLORS: Record<"high" | "med" | "low", string> = {
  high: "hsl(var(--success))",
  med: "hsl(var(--primary))",
  low: "hsl(var(--muted-foreground))",
}

// How many meta chips ride the resting card; the rest fold into the expanded
// tier behind a "+N" affordance.
const META_VISIBLE = 6

// Snippets carry exactly two inline highlight conventions: <mark> and <code>.
// Parse those tokens explicitly into elements; everything else (including any
// other tag) renders as literal text — no raw HTML injection.
const SNIPPET_TOKEN = /<(mark|code)>([\s\S]*?)<\/\1>/g

function renderSnippet(snippet: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  SNIPPET_TOKEN.lastIndex = 0
  while ((m = SNIPPET_TOKEN.exec(snippet)) !== null) {
    if (m.index > last) nodes.push(snippet.slice(last, m.index))
    const Tag = m[1] as "mark" | "code"
    nodes.push(<Tag key={m.index}>{m[2]}</Tag>)
    last = m.index + m[0].length
  }
  if (last < snippet.length) nodes.push(snippet.slice(last))
  return nodes
}

export function ResultCard({
  result,
  num,
  expanded,
  highlighted,
  sources,
  onToggle,
  onAskFollowUp,
}: ResultCardProps) {
  const src = sources.find((s) => s.id === result.source)
  const rel = relevanceBucket(result.relevance)
  const hasUrl = Boolean(result.url && result.url.trim())
  const href = hasUrl
    ? result.url.startsWith("http")
      ? result.url
      : `https://${result.url}`
    : undefined

  const chips = metaChips(result.meta)
  const restChips = chips.slice(0, META_VISIBLE)
  const overflowChips = chips.slice(META_VISIBLE)

  // The card is a reading surface, not a button — the TITLE links to the target
  // (the user expectation), and an explicit "More" affordance reveals the
  // expandable tier. Expandable content = overflow meta chips, an insight, refs,
  // or a long snippet. Nothing expandable ⇒ no More/Less button at all (the old
  // anti-pattern was a no-op toggle that revealed dead fixture data).
  const insight = result.insight ?? null
  const refs = result.refs && result.refs.length > 0 ? result.refs : null
  const longSnippet = result.snippet.length > 240
  const expandable =
    overflowChips.length > 0 || insight != null || refs != null || longSnippet

  return (
    <article
      id={`result-${result.id}`}
      className={cn(
        "group rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-3.5 py-2.5",
        "shadow-[var(--elev-1)] transition-[box-shadow,border-color] duration-150 ease-out",
        "hover:border-[hsl(var(--border-strong))] hover:shadow-[var(--elev-2)]",
        highlighted &&
          "ring-2 ring-[hsl(var(--primary))] ring-inset bg-[hsl(var(--primary)/0.06)]"
      )}
    >
      {/* Source identity row — classic search-result anatomy: rank · brand mark
          · source name · kind badge · relevance/confidence (right). */}
      <div className="flex items-center gap-2 text-xs text-[hsl(var(--muted-foreground))] min-w-0">
        <span className="font-mono tabular-nums w-4 text-right flex-none">{num}</span>
        <SrcAvatar source={src} size={16} />
        <span className="font-medium text-[hsl(var(--foreground))] truncate">
          {src?.name ?? result.source}
        </span>
        <span
          className="flex-none px-1.5 py-0.5 rounded text-[10px] font-medium uppercase tracking-wide bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]"
        >
          {KIND_LABEL[result.kind]}
        </span>
        <span className="ml-auto flex items-center gap-2 flex-none">
          {/* Agent-emitted cards carry the emitter's per-card confidence (#102);
              connector-era cards have none — render nothing, never a fake 0%.
              Accessible via aria-label (the old title-only tooltip was a sweep
              anti-pattern). */}
          {result.confidence != null && result.confidence > 0 && (
            <span
              className="font-mono tabular-nums text-[hsl(var(--muted-foreground))]"
              aria-label={`Agent confidence ${Math.round(result.confidence * 100)}%`}
            >
              {Math.round(result.confidence * 100)}%
            </span>
          )}
          {/* Relevance: an accessible labelled chip, not a title-only dot. */}
          <span
            className="inline-flex items-center gap-1 text-[10px] font-medium"
            style={{ color: REL_DOT_COLORS[rel] }}
            aria-label={`${REL_LABEL[rel]} (${Math.round(result.relevance * 100)}%)`}
          >
            <span
              className="inline-block h-1.5 w-1.5 rounded-full"
              style={{ background: REL_DOT_COLORS[rel] }}
            />
            <span>{Math.round(result.relevance * 100)}%</span>
          </span>
        </span>
      </div>

      {/* Title links to the target (new tab). Url-less cards (some connector
          hits) render the title as plain text — no dead "https://" link. */}
      <h3 className="mt-1 text-[15px] font-medium leading-snug">
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="text-[hsl(var(--foreground))] hover:text-[hsl(var(--primary))] hover:underline underline-offset-2 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))] rounded-sm"
          >
            {result.title}
          </a>
        ) : (
          <span className="text-[hsl(var(--foreground))]">{result.title}</span>
        )}
      </h3>

      {/* URL breadcrumb — suppressed entirely when empty (no dangling chrome). */}
      {hasUrl && (
        <div className="mt-0.5 text-[11px] text-[hsl(var(--success))] truncate">
          {result.url}
        </div>
      )}

      <p
        className={cn(
          "mt-1 text-[13.5px] leading-normal text-[hsl(var(--muted-foreground))]",
          "[&_mark]:bg-[hsl(var(--primary)/0.18)] [&_mark]:text-[hsl(var(--primary))] [&_mark]:rounded-sm [&_mark]:px-0.5 [&_code]:font-mono [&_code]:text-[12.5px] [&_code]:bg-[hsl(var(--muted))] [&_code]:px-1 [&_code]:py-0.5 [&_code]:rounded",
          !expanded && longSnippet && "line-clamp-3"
        )}
      >
        {renderSnippet(result.snippet)}
      </p>

      {/* Meta chip row — structured facts rendered dynamically from `meta`. */}
      {restChips.length > 0 && (
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          {restChips.map((c) => (
            <MetaChipView key={c.key} chip={c} />
          ))}
          {!expanded && overflowChips.length > 0 && (
            <span className="text-[11px] text-[hsl(var(--muted-foreground))] px-1">
              +{overflowChips.length}
            </span>
          )}
        </div>
      )}

      {/* Expanded tier — overflow meta, insight, refs. Image/embed chrome was
          fixture-only and is gone; insight + refs are legitimately data-gated. */}
      {expanded && overflowChips.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {overflowChips.map((c) => (
            <MetaChipView key={c.key} chip={c} />
          ))}
        </div>
      )}

      {expanded && refs && (
        <div className="mt-2.5 border-l-2 border-[hsl(var(--border-strong))] pl-3 space-y-1.5">
          <div className="text-[11px] uppercase tracking-wider font-mono text-[hsl(var(--muted-foreground))]">
            Referenced ({refs.length})
          </div>
          {refs.map((ref) => (
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

      {expanded && insight && (
        <div className="mt-2.5 flex gap-2 p-3 rounded-md bg-[hsl(var(--permission)/0.08)] border border-[hsl(var(--permission)/0.25)]">
          <Sparkles className="h-4 w-4 text-[hsl(var(--permission))] flex-none mt-0.5" />
          <div className="text-[13px]">
            <div className="text-[11px] uppercase tracking-wider font-mono text-[hsl(var(--muted-foreground))] mb-0.5">
              {insight.label}
            </div>
            <div className="leading-relaxed">{insight.body}</div>
          </div>
        </div>
      )}

      {/* Footer — author/timestamp render ONLY when present (no dangling "·").
          The action cluster is keyboard-reachable; it reveals on hover AND
          group-focus-within so it's never mouse-only. */}
      <footer className="flex items-center gap-2 mt-1.5 text-xs text-[hsl(var(--muted-foreground))] min-h-6">
        {result.author && result.author.trim() && (
          <span className="truncate">{result.author}</span>
        )}
        {result.timestamp && result.timestamp.trim() && (
          <span className="font-mono text-[11px] truncate">{result.timestamp}</span>
        )}
        {expandable && (
          <button
            type="button"
            onClick={onToggle}
            aria-expanded={expanded}
            className="ml-auto inline-flex items-center gap-1 px-2 h-6 rounded hover:bg-[hsl(var(--accent))] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]"
          >
            {expanded ? "Less" : "More"}
            {expanded ? <ChevronUp className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
        )}
        <span
          className={cn(
            "inline-flex items-center gap-0.5 opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 focus-within:opacity-100 transition-opacity",
            !expandable && "ml-auto"
          )}
        >
          {href && (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              title="Open in new tab"
              aria-label="Open in new tab"
              className="inline-flex items-center justify-center h-6 w-6 rounded hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]"
            >
              <ExternalLink className="h-3 w-3" />
            </a>
          )}
          {/* Copy the external source URL — only meaningful when there is one. */}
          {href && <CopyButton text={href} label="Copy link" className="h-6 w-6 rounded" />}
          {onAskFollowUp && (
            <button
              type="button"
              onClick={() => onAskFollowUp(result)}
              title="Ask a follow-up about this result"
              aria-label="Ask a follow-up about this result"
              className="inline-flex items-center justify-center h-6 w-6 rounded hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))] transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]"
            >
              <Sparkles className="h-3 w-3" />
            </button>
          )}
        </span>
      </footer>
    </article>
  )
}

// status tone → CSS theme token (never a hardcoded colour; both themes define
// these). "neutral" reuses the muted-foreground so an unknown status still
// renders as a calm badge rather than an invented colour.
const STATUS_TONE_TOKEN: Record<StatusTone, string> = {
  positive: "--success",
  done: "--permission",
  negative: "--destructive",
  pending: "--warning",
  neutral: "--muted-foreground",
}

/** One structured-meta chip: count (icon + compact number), time, status, tag. */
function MetaChipView({ chip }: { chip: MetaChip }) {
  const { Icon, kind, label, value } = chip

  // A status/state badge is colour-coded by tone (open/merged/failed/draft) so
  // a ticket or PR's state reads at a glance — the headline footer signal.
  if (kind === "status") {
    const token = STATUS_TONE_TOKEN[chip.tone ?? "neutral"]
    return (
      <span
        className="inline-flex items-center gap-1 px-1.5 h-5 rounded text-[11px] font-medium"
        style={{ background: `hsl(var(${token}) / 0.14)`, color: `hsl(var(${token}))` }}
        title={`${label}: ${value}`}
      >
        <span
          className="inline-block h-1.5 w-1.5 rounded-full"
          style={{ background: `hsl(var(${token}))` }}
        />
        {value}
      </span>
    )
  }

  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 px-1.5 h-5 rounded text-[11px] font-medium",
        "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]"
      )}
      title={`${label}: ${value}`}
    >
      {Icon && <Icon className="h-3 w-3 flex-none opacity-70" />}
      {kind === "count" ? (
        <span className="font-mono tabular-nums text-[hsl(var(--foreground))]">{value}</span>
      ) : kind === "time" ? (
        <span>{value}</span>
      ) : (
        // Tag/labelled chips name their key so an unknown fact reads as
        // "label: value" rather than a bare value.
        <>
          {!Icon && <span className="opacity-70">{label}</span>}
          <span className="text-[hsl(var(--foreground))]">{value}</span>
        </>
      )}
    </span>
  )
}
