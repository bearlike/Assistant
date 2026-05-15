/**
 * Renders a parsed markdown body via `react-markdown` + `remark-gfm` +
 * `rehype-slug` (for heading anchor ids that match our auto-derived TOC).
 *
 * Custom component handlers wire the wiki's special atoms:
 *   - ```mermaid fenced code → MermaidBlock (lazy-loaded, click-to-zoom)
 *   - `[label](src:path/to.py#L12-44)` → SrcChip with line range
 *   - relative anchor-style links `[Title](page-id)` → internal page navigation
 *
 * Frontmatter side-content (relevantSources accordion, trailing sources
 * block) is rendered around the body — kept structured because it is
 * conceptually metadata, not prose.
 */

import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeSlug from "rehype-slug";
import { ChevronRight, FileText, Github } from "lucide-react";

import { cn } from "@/lib/utils";

import { MermaidBlock } from "./MermaidBlock";
import type { PageFrontmatter } from "./api/markdown";

/**
 * Deterministic id from a string. Used so the same mermaid block produces
 * the same React key (and the same diagram id) across re-renders — without
 * that, scroll-spy triggers re-renders that hand mermaid fresh ids, mermaid
 * re-renders concurrently into the same DOM nodes, and the page flickers
 * and jumps while scrolling. djb2 is enough for collision-free ids inside
 * a single page.
 */
function stableId(src: string, prefix: string): string {
  let h = 5381;
  for (let i = 0; i < src.length; i++) {
    h = ((h << 5) + h + src.charCodeAt(i)) | 0;
  }
  return `${prefix}-${(h >>> 0).toString(36)}`;
}

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
  // Component handler maps are memoised by the markdown body so react-markdown
  // doesn't re-walk the AST on every parent re-render (scroll-spy ticks the
  // active TOC heading once per scroll event). Without this, every scroll
  // event re-mounts the mermaid subtree.
  const components = useMemo<Components>(() => ({
    // ── Mermaid fenced code blocks ──────────────────────────────────
    code({ className, children, ...rest }) {
      const lang = /language-(\w+)/.exec(className ?? "")?.[1];
      const text = String(children ?? "").replace(/\n$/, "");
      if (lang === "mermaid") {
        // Stable id derived from the source — same diagram → same id across
        // every render, so MermaidBlock's reconciler keeps the rendered SVG.
        const id = stableId(text, "wiki-d");
        return (
          <MermaidBlock
            key={id}
            diagramId={id}
            inlineSource={text}
            onZoom={() => onZoomDiagram(id)}
          />
        );
      }
      const isBlock = (rest as { node?: { position?: { start: { line: number }, end: { line: number } } } }).node?.position?.start.line !==
        (rest as { node?: { position?: { start: { line: number }, end: { line: number } } } }).node?.position?.end.line;
      if (isBlock) {
        return (
          <pre className="my-4 overflow-x-auto rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] p-3 text-[12.5px] font-mono leading-[1.55]">
            <code className={cn("hljs", className)}>{text}</code>
          </pre>
        );
      }
      return (
        <code className="font-mono text-[0.85em] px-1 py-0.5 rounded bg-[hsl(var(--muted))]/70 text-[hsl(var(--foreground))]">
          {children}
        </code>
      );
    },

    // ── Links: detect `src:` chips and relative internal page links ─
    a({ href, children, ...rest }) {
      if (href?.startsWith("src:")) {
        const stripped = href.slice(4);
        const [path, range] = stripped.split("#");
        const lines = range?.startsWith("L") ? range.slice(1) : range;
        return <SrcChip path={path} lines={lines} />;
      }
      if (href && !/^[a-z]+:|^\/|^#/.test(href)) {
        return (
          <button
            type="button"
            onClick={() => onNavigatePage(href)}
            className="inline text-[hsl(var(--primary))] hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit text-[14.5px]"
          >
            {children}
          </button>
        );
      }
      return (
        <a
          href={href}
          target={href?.startsWith("http") ? "_blank" : undefined}
          rel={href?.startsWith("http") ? "noopener noreferrer" : undefined}
          className="text-[hsl(var(--primary))] hover:underline underline-offset-2"
          {...rest}
        >
          {children}
        </a>
      );
    },

    // ── Typography ──────────────────────────────────────────────────
    h1({ children, ...rest }) {
      return (
        <h1
          {...rest}
          className="text-[clamp(24px,3vw,30px)] font-semibold tracking-[-0.02em] mt-1 mb-6 [text-wrap:balance]"
        >
          {children}
        </h1>
      );
    },
    h2({ children, ...rest }) {
      return (
        <h2
          {...rest}
          className="text-[22px] font-semibold tracking-[-0.02em] mt-10 mb-3 scroll-mt-20"
        >
          {children}
        </h2>
      );
    },
    h3({ children, ...rest }) {
      return (
        <h3
          {...rest}
          className="text-[16px] font-semibold tracking-tight mt-7 mb-2 scroll-mt-20"
        >
          {children}
        </h3>
      );
    },
    p({ children, ...rest }) {
      return (
        <p
          {...rest}
          className="text-[14.5px] leading-[1.7] text-[hsl(var(--foreground))] [text-wrap:pretty] my-4"
        >
          {children}
        </p>
      );
    },
    ul({ children, ...rest }) {
      return (
        <ul
          {...rest}
          className="my-4 space-y-1.5 list-disc pl-5 marker:text-[hsl(var(--muted-foreground))] text-[14.5px] leading-[1.7]"
        >
          {children}
        </ul>
      );
    },
    ol({ children, ...rest }) {
      return (
        <ol
          {...rest}
          className="my-4 space-y-1.5 list-decimal pl-5 marker:text-[hsl(var(--muted-foreground))] text-[14.5px] leading-[1.7]"
        >
          {children}
        </ol>
      );
    },
    li({ children, ...rest }) {
      return (
        <li {...rest} className="[text-wrap:pretty]">
          {children}
        </li>
      );
    },
    hr() {
      return <hr className="my-8 border-0 border-t border-[hsl(var(--border))]" />;
    },
    blockquote({ children, ...rest }) {
      return (
        <blockquote
          {...rest}
          className="my-5 border-l-2 border-[hsl(var(--primary))]/40 bg-[hsl(var(--muted))]/30 pl-4 pr-3 py-2 rounded-r text-sm text-[hsl(var(--muted-foreground))] [text-wrap:pretty]"
        >
          {children}
        </blockquote>
      );
    },
    table({ children, ...rest }) {
      return (
        <div className="my-5 overflow-x-auto rounded-md border border-[hsl(var(--border))]">
          <table {...rest} className="w-full text-sm">
            {children}
          </table>
        </div>
      );
    },
    thead({ children, ...rest }) {
      return (
        <thead {...rest} className="bg-[hsl(var(--muted))]/50">
          {children}
        </thead>
      );
    },
    th({ children, ...rest }) {
      return (
        <th
          {...rest}
          className="text-left font-medium text-xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] px-3 py-2"
        >
          {children}
        </th>
      );
    },
    tr({ children, ...rest }) {
      return (
        <tr
          {...rest}
          className="border-t border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]/30"
        >
          {children}
        </tr>
      );
    },
    td({ children, ...rest }) {
      return (
        <td {...rest} className="px-3 py-2.5 align-top [text-wrap:pretty]">
          {children}
        </td>
      );
    },
  }), [onNavigatePage, onZoomDiagram]);

  return (
    <>
      {frontmatter.relevantSources?.length ? (
        <Accordion title="Relevant source files" items={frontmatter.relevantSources} />
      ) : null}
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSlug]}
        components={components}
      >
        {body}
      </ReactMarkdown>
      {frontmatter.sources?.length ? <SourcesBlock items={frontmatter.sources} /> : null}
    </>
  );
}

// ── Inline atoms ────────────────────────────────────────────────────

function SrcChip({ path, lines }: { path: string; lines?: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-px rounded font-mono text-[11px] bg-[hsl(var(--muted))]/60 text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors align-baseline"
      title={path}
    >
      <Github className="h-2.5 w-2.5" />
      <span className="truncate max-w-[260px]">{path}</span>
      {lines && (
        <span className="text-[10px] text-[hsl(var(--muted-foreground))]/80">{lines}</span>
      )}
    </span>
  );
}

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
            <SrcChip path={it.path} lines={it.lines} />
          </li>
        ))}
      </ul>
    </div>
  );
}
