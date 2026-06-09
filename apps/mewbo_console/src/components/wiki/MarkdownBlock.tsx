/**
 * Renders a parsed markdown body via `react-markdown` + `remark-gfm` +
 * `rehype-highlight` (syntax highlighting) + `rehype-slug` (heading anchor
 * ids that match our auto-derived TOC).
 *
 * The component map (headings, lists, tables, code, citation chips, internal
 * links) lives in the shared `markdownComponents` module so this full page
 * renderer and the streaming Q&A renderer (`LiveBlocks`) stay byte-identical
 * — there is exactly ONE renderer for the wiki. Mermaid is enabled here
 * (wiki pages embed diagrams) and disabled for Q&A.
 *
 * Frontmatter side-content (relevantSources accordion, trailing sources
 * block) is rendered around the body — kept structured because it is
 * conceptually metadata, not prose.
 */

import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeSlug from "rehype-slug";
import { ChevronRight, FileText } from "lucide-react";

import { CitationRef } from "./citations";
import { SrcChip, buildMarkdownComponents } from "./markdownComponents";
import type { PageFrontmatter } from "./api/markdown";

interface MarkdownBlockProps {
  body: string;
  frontmatter: PageFrontmatter;
  onNavigatePage: (pageId: string) => void;
  onZoomDiagram: (id: string) => void;
}

export function MarkdownBlock({
  body,
  frontmatter,
  onNavigatePage,
  onZoomDiagram,
}: MarkdownBlockProps) {
  // Memoised by its callbacks so react-markdown doesn't re-walk the AST on
  // every parent re-render (scroll-spy ticks the active TOC heading once per
  // scroll event); without this, every scroll re-mounts the mermaid subtree.
  const components = useMemo(
    () => buildMarkdownComponents({ onNavigatePage, onZoomDiagram, enableMermaid: true }),
    [onNavigatePage, onZoomDiagram],
  );

  return (
    <>
      {frontmatter.relevantSources?.length ? (
        <Accordion title="Relevant source files" items={frontmatter.relevantSources} />
      ) : null}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight, rehypeSlug]}
        components={components}
      >
        {body}
      </ReactMarkdown>
      {frontmatter.sources?.length ? <SourcesBlock items={frontmatter.sources} /> : null}
    </>
  );
}

// ── Frontmatter side-content ────────────────────────────────────────────

function Accordion({
  title,
  items,
}: {
  title: string;
  items: Array<{ path: string; lines?: string }>;
}) {
  return (
    <details className="group my-5 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]/40 overflow-hidden">
      <summary className="flex items-center gap-2 px-3.5 py-2.5 cursor-pointer select-none list-none text-sm font-medium text-[hsl(var(--foreground))] hover:bg-[hsl(var(--muted))]/30">
        <ChevronRight className="h-3.5 w-3.5 transition-transform group-open:rotate-90 text-[hsl(var(--muted-foreground))]" />
        {title}
      </summary>
      <div className="border-t border-[hsl(var(--border))] px-3.5 py-2 space-y-1">
        {items.map((it, i) => (
          <div
            key={i}
            className="flex items-center gap-2 font-mono text-xs text-[hsl(var(--muted-foreground))] py-1"
          >
            <FileText className="h-3 w-3" />
            <span className="truncate flex-1">{it.path}</span>
            {it.lines && <span className="text-[10px]">{it.lines}</span>}
          </div>
        ))}
      </div>
    </details>
  );
}

function SourcesBlock({ items }: { items: Array<{ path: string; lines?: string }> }) {
  return (
    <div className="my-5 px-3.5 py-2.5 rounded-md bg-[hsl(var(--muted))]/30 border border-[hsl(var(--border))]">
      <div className="text-[10px] uppercase tracking-wide font-medium text-[hsl(var(--muted-foreground))] mb-1.5">
        Sources
      </div>
      <ul className="flex flex-wrap gap-1.5">
        {items.map((it, i) => (
          <li key={i}>
            <SrcChip citation={CitationRef.fromSrc(it.path, it.lines)} />
          </li>
        ))}
      </ul>
    </div>
  );
}
