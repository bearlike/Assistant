/**
 * One cited-source card for the Q&A right panel — a collapsible viewer that
 * lazily fetches a file excerpt and renders it with line numbers, the cited
 * range highlighted. Atomic: it owns its own fetch + collapse state.
 *
 * The card mounts under a stable DOM id (`CitationRef.domId`) so inline
 * citation chips in the answer can scroll to + flash it (see `SrcChip`).
 *
 * Uses a native `<details open>` (the wiki's established collapsible idiom —
 * no new shadcn dep) and the shared `--code-*` surface tokens so it themes
 * with the rest of the code-display surfaces.
 */

import { ChevronRight, FileCode2, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";

import { CitationRef, type Citation } from "./citations";
import { useSourceExcerpt } from "./api/hooks";

interface SourceCardProps {
  citation: Citation;
  slug: string;
}

export function SourceCard({ citation, slug }: SourceCardProps) {
  const { data, isLoading, isError, error } = useSourceExcerpt(
    slug,
    citation.path,
    citation.startLine,
    citation.endLine,
  );

  const domId = CitationRef.domId(citation);
  const header = CitationRef.label(citation);

  return (
    <details
      id={domId}
      open
      className="group rounded-md border border-[hsl(var(--code-border))] bg-[hsl(var(--code-body))] overflow-hidden scroll-mt-20 transition-shadow"
    >
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer select-none list-none bg-[hsl(var(--code-chrome))] hover:bg-[hsl(var(--muted))]/30">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90 text-[hsl(var(--code-fg-muted))]" />
        <FileCode2 className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--code-fg-muted))]" />
        <span className="font-mono text-[11.5px] text-[hsl(var(--code-fg))] truncate" title={citation.path}>
          {header}
        </span>
      </summary>
      <div className="border-t border-[hsl(var(--code-border))]">
        {isLoading ? (
          <div className="flex items-center gap-2 px-3 py-3 text-[11px] text-[hsl(var(--code-fg-muted))]">
            <Loader2 className="h-3 w-3 animate-spin" />
            Loading excerpt…
          </div>
        ) : isError ? (
          <div className="px-3 py-3 text-[11px] text-[hsl(var(--code-stderr))]">
            {error instanceof Error ? error.message : "Couldn’t load this source."}
          </div>
        ) : data ? (
          <ExcerptBody
            content={data.content}
            firstLine={data.startLine ?? 1}
            highlightStart={citation.startLine}
            highlightEnd={citation.endLine ?? citation.startLine}
          />
        ) : null}
      </div>
    </details>
  );
}

/**
 * Line-numbered code body. ``firstLine`` is the 1-based number of the first
 * rendered line (so the gutter is correct even for a windowed excerpt); the
 * cited ``highlightStart..highlightEnd`` lines get an accent rail + tint.
 */
function ExcerptBody({
  content,
  firstLine,
  highlightStart,
  highlightEnd,
}: {
  content: string;
  firstLine: number;
  highlightStart: number | null;
  highlightEnd: number | null;
}) {
  // Drop a single trailing newline so we don't render a blank final row.
  const lines = content.replace(/\n$/, "").split("\n");
  const gutterWidth = String(firstLine + lines.length - 1).length;

  return (
    <pre className="overflow-x-auto text-[12px] font-mono leading-[1.55] text-[hsl(var(--code-fg))] py-1.5">
      <code>
        {lines.map((line, i) => {
          const lineNo = firstLine + i;
          const highlighted =
            highlightStart != null &&
            lineNo >= highlightStart &&
            lineNo <= (highlightEnd ?? highlightStart);
          return (
            <span
              key={i}
              className={cn(
                "grid grid-cols-[auto_1fr] gap-3 px-3",
                highlighted &&
                  "bg-[hsl(var(--primary))]/10 border-l-2 border-[hsl(var(--primary))] -ml-px",
              )}
            >
              <span
                className="select-none text-right tabular-nums text-[hsl(var(--code-fg-subtle))]"
                style={{ minWidth: `${gutterWidth}ch` }}
              >
                {lineNo}
              </span>
              <span className="whitespace-pre">{line || " "}</span>
            </span>
          );
        })}
      </code>
    </pre>
  );
}
