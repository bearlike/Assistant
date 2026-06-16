import { ArrowUpRight, Sparkles } from "lucide-react"
import ReactMarkdown from "react-markdown"
import remarkGfm from "remark-gfm"
import rehypeHighlight from "rehype-highlight"
import rehypeSlug from "rehype-slug"

import { cn } from "@/lib/utils"

import { CopyButton } from "../CopyButton"
import { buildMarkdownComponents } from "../wiki/markdownComponents"
import type { RunAnswer, SearchResult, SourceCatalogEntry } from "../../types/agenticSearch"
import { SrcAvatar } from "./SrcAvatar"

// The synthesis is LLM-authored markdown (bullets, bold, inline `code` for
// source_keys/ids). Reuse the console's ONE markdown renderer (the wiki-QA
// single-renderer rule — see wiki/CLAUDE.md "one renderer") instead of
// hand-rolling a second. A search synthesis has no wiki-page links, so
// onNavigatePage is a no-op and mermaid stays off. Built once at module scope
// (it depends only on the callbacks) per the console "components map outside the
// render function" rule.
const SYNTHESIS_MD = buildMarkdownComponents({
  onNavigatePage: () => undefined, // no wiki-page links in a search synthesis
  enableMermaid: false,
})

/** Render the synthesis tldr as markdown, with an optional streaming cursor. */
function SynthesisMarkdown({ source, cursor }: { source: string; cursor?: boolean }) {
  return (
    <div className="max-w-[64ch] [&>*:first-child]:mt-0 [&>*:last-child]:mb-0">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, rehypeSlug]}
        components={SYNTHESIS_MD}
      >
        {source}
      </ReactMarkdown>
      {cursor && (
        <span className="inline-block animate-pulse text-[hsl(var(--primary))]">▌</span>
      )}
    </div>
  )
}

/** Render the answer as Markdown for the clipboard (tldr + bullet list). */
function answerToMarkdown(answer: RunAnswer): string {
  const bullets = answer.bullets.map((b) => `- ${b.text}`).join("\n")
  return bullets ? `${answer.tldr}\n\n${bullets}` : answer.tldr
}

interface AnswerCardProps {
  answer: RunAnswer
  results: SearchResult[]
  sources: SourceCatalogEntry[]
  /** Final cited synthesis has landed (`answer_ready`). */
  ready: boolean
  /** Run reached a terminal state — stops the streaming affordances even
   *  when no final answer ever landed (failed / cancelled mid-synthesis). */
  done: boolean
  /** Real elapsed ms since run start — display only. */
  elapsedMs: number
  onCiteClick: (resultId: string) => void
  onAsk: () => void
}

export function AnswerCard({
  answer,
  results,
  sources,
  ready,
  done,
  elapsedMs,
  onCiteClick,
  onAsk,
}: AnswerCardProps) {
  // The tldr streams in via `answer_delta` even before `ready`; bullets are
  // only meaningful once the final cited block lands (`answer_ready`).
  // A terminal run without `answer_ready` (cancelled / failed mid-synthesis)
  // renders its partial tldr statically — no live cursor, no pulse.
  const streaming = answer.tldr.length > 0 && !ready && !done
  const partial = !ready && done
  const visibleBullets = ready ? answer.bullets.length : 0

  return (
    <section
      className={cn(
        "relative rounded-xl border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 shadow-[var(--elev-2)]",
        "before:absolute before:left-0 before:top-3 before:bottom-3 before:w-[3px] before:rounded-r before:bg-[hsl(var(--primary))]",
        !ready && !done && "before:animate-pulse"
      )}
    >
      <header className="flex items-start gap-3 mb-2.5">
        <div className="flex items-center gap-2 flex-1">
          <span className="inline-flex items-center justify-center h-6 w-6 rounded-md bg-[hsl(var(--primary)/0.15)] text-[hsl(var(--primary))]">
            <Sparkles className="h-3.5 w-3.5" />
          </span>
          <span className="text-sm font-semibold">Synthesis</span>
          <span className="text-xs font-mono text-[hsl(var(--muted-foreground))]">
            {ready
              ? `${answer.sources_count} sources · ${(elapsedMs / 1000).toFixed(1)}s`
              : partial
              ? "partial"
              : "synthesising…"}
          </span>
        </div>
        <div className="flex items-center gap-1 text-[hsl(var(--muted-foreground))]">
          {/* Copies the synthesized answer as Markdown (tldr + bullets).
              Sharing the run lives in the ResultsPanel header "Copy link". */}
          <CopyButton text={answerToMarkdown(answer)} label="Copy answer" />
        </div>
      </header>

      {partial && answer.tldr.length > 0 ? (
        // Terminal-without-final-answer: the partial tldr, frozen.
        <SynthesisMarkdown source={answer.tldr} />
      ) : !ready && !streaming ? (
        <div className="space-y-2">
          <SkeletonLine width="90%" />
          <SkeletonLine width="70%" />
          <SkeletonLine width="60%" />
        </div>
      ) : !ready && streaming ? (
        // Typewriter: tldr tokens arriving via `answer_delta`, bullets pending.
        <SynthesisMarkdown source={answer.tldr} cursor />
      ) : (
        <>
          <SynthesisMarkdown source={answer.tldr} />
          <ul className="mt-3 space-y-2">
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
            <footer className="flex items-center gap-3 mt-4 pt-2.5 border-t border-[hsl(var(--border))]">
              {/* Confidence is a settle-derived heuristic (data-bearing probes /
                  probes run). 0 means "no probe ran" → unknown, NOT zero faith;
                  suppress the chip rather than render an unearned 0%. */}
              {answer.confidence > 0 && <ConfidenceBar confidence={answer.confidence} />}
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

function SkeletonLine({ width }: { width: string }) {
  return (
    <div
      className="h-3.5 rounded bg-[hsl(var(--muted))] animate-pulse"
      style={{ width }}
    />
  )
}

// Map a confidence value to a semantic tier + token. We don't have a --warning
// token; --primary is the brand clay (orange-amber) which fits "medium", and
// --agent-3 is yellow which fits "low".
function confidenceTier(pct: number): { color: string; label: string } {
  if (pct >= 80) return { color: "hsl(var(--success))", label: "High confidence" }
  if (pct >= 60) return { color: "hsl(var(--primary))", label: "Medium confidence" }
  if (pct >= 40) return { color: "hsl(var(--agent-3))", label: "Low confidence" }
  return { color: "hsl(var(--destructive))", label: "Low confidence" }
}

// A calm labelled pill — not a debug-looking colored progress bar. A small
// tinted dot + a worded tier (not a bare "100%" green readout) reads as a
// provenance signal, not telemetry.
function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100)
  const { color, label } = confidenceTier(pct)
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))] flex-1"
      aria-label={`${label} (${pct}%)`}
    >
      <span
        className="inline-block h-2 w-2 rounded-full flex-none"
        style={{ background: color }}
      />
      <span>{label}</span>
      <span className="font-mono tabular-nums text-[11px] opacity-70">{pct}%</span>
    </span>
  )
}
