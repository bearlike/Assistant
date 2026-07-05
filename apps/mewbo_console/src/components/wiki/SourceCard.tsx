/**
 * One cited-source card for the Q&A right panel — a collapsible viewer that
 * resolves a citation to its content. The card branches on the citation kind:
 *
 *   - ``file``  — lazily fetches a line-numbered file excerpt, cited range
 *                 highlighted (the original behaviour).
 *   - ``page``  — fetches that ONE documentation page by id and renders its
 *                 title + a short text excerpt (never lists the whole page set).
 *   - ``graph`` — renders a compact resolved label + the node id, no fetch.
 *
 * Atomic: each branch owns its own fetch + collapse state. The card mounts
 * under a stable DOM id (`citationDomId`) so inline citation chips in the
 * answer can scroll to + flash it (see `SrcChip`).
 *
 * Uses a native `<details open>` (the wiki's established collapsible idiom —
 * no new shadcn dep) and the shared `--code-*` surface tokens so it themes
 * with the rest of the code-display surfaces.
 */

import type { ReactNode } from "react";
import { ChevronRight, ExternalLink, FileCode2, FileText, Loader2, Network } from "lucide-react";

import { cn } from "@/lib/utils";

import {
  asParsedCitation,
  citationDomId,
  citationLabel,
  type Citation,
  type ParsedCitation,
} from "./citations";
import type { IndexedSnapshot } from "./indexedSnapshot";
import { buildHref } from "./router";
import { useSourceExcerpt, useWikiPage } from "./api/hooks";

interface SourceCardProps {
  /**
   * Accepts the legacy flat {@link Citation} (QAScreen's `fileCitations`) or the
   * kind-tagged {@link ParsedCitation} (`parseCitations`) — both render.
   */
  citation: Citation | ParsedCitation;
  slug: string;
  /**
   * The repo snapshot, when known — lets a `file` card link its header out to
   * the cited file in the remote repository (host-aware blob URL, new tab).
   */
  snapshot?: IndexedSnapshot | null;
}

export function SourceCard({ citation, slug, snapshot }: SourceCardProps) {
  const parsed = asParsedCitation(citation);
  switch (parsed.kind) {
    case "page":
      return <PageSourceCard citation={parsed} slug={slug} />;
    case "graph":
      return <GraphSourceCard citation={parsed} />;
    default:
      return <FileSourceCard citation={parsed} slug={slug} snapshot={snapshot} />;
  }
}

/** Shared collapsible chrome so every card kind reads the same. */
function CardShell({
  domId,
  icon,
  header,
  href,
  openLabel,
  children,
}: {
  domId: string;
  icon: ReactNode;
  header: string;
  /** When set, an external-link affordance opens this URL in a new tab. */
  href?: string | null;
  /** Accessible label for the open-in-new-tab affordance. */
  openLabel?: string;
  children: ReactNode;
}) {
  return (
    <details
      id={domId}
      open
      className="group rounded-md border border-[hsl(var(--code-border))] bg-[hsl(var(--code-body))] overflow-hidden scroll-mt-20 transition-shadow"
    >
      <summary className="flex items-center gap-2 px-3 py-2 cursor-pointer select-none list-none bg-[hsl(var(--code-chrome))] hover:bg-[hsl(var(--muted))]/30">
        <ChevronRight className="h-3 w-3 shrink-0 transition-transform group-open:rotate-90 text-[hsl(var(--code-fg-muted))]" />
        {icon}
        <span className="font-mono text-[11.5px] text-[hsl(var(--code-fg))] truncate" title={header}>
          {header}
        </span>
        {href && (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            // Stop the click from toggling the enclosing <details>.
            onClick={(e) => e.stopPropagation()}
            aria-label={openLabel ?? "Open source"}
            title={openLabel ?? "Open source"}
            className="ml-auto shrink-0 inline-flex items-center justify-center h-5 w-5 rounded text-[hsl(var(--code-fg-muted))] hover:text-[hsl(var(--code-fg))] hover:bg-[hsl(var(--muted))]/40"
          >
            <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </summary>
      <div className="border-t border-[hsl(var(--code-border))]">{children}</div>
    </details>
  );
}

/** Muted "couldn't resolve this source" line — never raw stderr-red text. */
function Unavailable() {
  return (
    <div className="px-3 py-3 text-[11px] text-[hsl(var(--code-fg-muted))]">
      Source unavailable.
    </div>
  );
}

function Loading() {
  return (
    <div className="flex items-center gap-2 px-3 py-3 text-[11px] text-[hsl(var(--code-fg-muted))]">
      <Loader2 className="h-3 w-3 animate-spin" />
      Loading excerpt…
    </div>
  );
}

/** A cited repo file — lazily fetched, line-numbered, cited range highlighted. */
function FileSourceCard({
  citation,
  slug,
  snapshot,
}: {
  citation: Extract<ParsedCitation, { kind: "file" }>;
  slug: string;
  snapshot?: IndexedSnapshot | null;
}) {
  const { data, isLoading, isError } = useSourceExcerpt(
    slug,
    citation.path,
    citation.startLine,
    citation.endLine,
  );
  const repoHref = snapshot?.sourceUrl(citation.path, citation.startLine, citation.endLine) ?? null;

  return (
    <CardShell
      domId={citationDomId(citation)}
      header={citationLabel(citation)}
      href={repoHref}
      openLabel="Open file in repository"
      icon={<FileCode2 className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--code-fg-muted))]" />}
    >
      {isLoading ? (
        <Loading />
      ) : isError || !data ? (
        <Unavailable />
      ) : (
        <ExcerptBody
          content={data.content}
          firstLine={data.startLine ?? 1}
          highlightStart={citation.startLine}
          highlightEnd={citation.endLine ?? citation.startLine}
        />
      )}
    </CardShell>
  );
}

/**
 * A cited documentation page — fetched ONCE by id via the shared single-page
 * hook (never a list-all-pages resolve, per the wiki design rule). Renders the
 * page title + a short prose excerpt.
 */
function PageSourceCard({
  citation,
  slug,
}: {
  citation: Extract<ParsedCitation, { kind: "page" }>;
  slug: string;
}) {
  const { data, isLoading, isError } = useWikiPage(citation.pageId, slug);
  const header = citation.title ?? data?.title ?? citation.pageId;

  return (
    <CardShell
      domId={citationDomId(citation)}
      header={header}
      href={buildHref({ kind: "page", pageId: citation.pageId, slug })}
      openLabel="Open wiki page"
      icon={<FileText className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--code-fg-muted))]" />}
    >
      {isLoading ? (
        <Loading />
      ) : isError || !data ? (
        <Unavailable />
      ) : (
        <p className="px-3 py-2.5 text-[11.5px] leading-[1.55] text-[hsl(var(--code-fg-muted))]">
          {pageExcerpt(data.body)}
        </p>
      )}
    </CardShell>
  );
}

/**
 * A cited code-graph node — rendered as a compact resolved label (the node id
 * is often already ``file#Symbol`` / ``name (type)``). No fetch.
 */
function GraphSourceCard({
  citation,
}: {
  citation: Extract<ParsedCitation, { kind: "graph" }>;
}) {
  return (
    <CardShell
      domId={citationDomId(citation)}
      header={citationLabel(citation)}
      icon={<Network className="h-3.5 w-3.5 shrink-0 text-[hsl(var(--code-fg-muted))]" />}
    >
      <p className="px-3 py-2.5 text-[11px] text-[hsl(var(--code-fg-muted))]">
        Code-graph node{" "}
        <span className="font-mono text-[hsl(var(--code-fg))]">{citation.nodeId}</span>
      </p>
    </CardShell>
  );
}

/**
 * Condense a markdown page body to a short, single-paragraph excerpt: drop a
 * leading H1, collapse whitespace, and clamp to a readable length.
 */
function pageExcerpt(body: string, max = 320): string {
  const text = body
    .replace(/^#.*$/m, "") // drop a leading heading line
    .replace(/[#>*_`]/g, "") // strip the lightest markdown punctuation
    .replace(/\s+/g, " ")
    .trim();
  if (text.length <= max) return text;
  return `${text.slice(0, max).trimEnd()}…`;
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
