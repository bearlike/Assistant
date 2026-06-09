/* eslint-disable react-refresh/only-export-components -- this is a renderer
   module, not a fast-refresh component file: it intentionally exports the
   shared `SrcChip` component alongside the `buildMarkdownComponents` factory
   so both wiki renderers consume one source of truth. */
/**
 * Shared react-markdown renderer for the wiki — one component map + one set
 * of inline atoms used by BOTH the full page renderer (`MarkdownBlock`) and
 * the streaming Q&A renderer (`LiveBlocks`). DRY: there is exactly one place
 * that decides how a heading / list / table / code-block / citation link
 * looks across the wiki.
 *
 * The map is parameterised by callbacks (internal-page navigation, mermaid
 * zoom) so each consumer wires its own behaviour without forking the styles.
 * Mermaid is opt-in (`enableMermaid`) — Q&A answers never contain diagrams,
 * so `LiveBlocks` leaves it off and the mermaid module never loads there.
 *
 * Citation chips: a `[label](src:path#L1-9)` link (or a `path:line` /
 * `path#L..` bare-text fallback) renders as an accent CHIP. Clicking it
 * scrolls to + briefly highlights the matching `SourceCard` via the shared
 * `CitationRef.domId` id — no prop threading; the card finds itself by id.
 */

import { Github } from "lucide-react";
import type { Components } from "react-markdown";

import { cn } from "@/lib/utils";

import { MermaidBlock } from "./MermaidBlock";
import { CitationRef, type Citation } from "./citations";

/**
 * Deterministic id from a string. Used so the same mermaid block produces
 * the same React key (and the same diagram id) across re-renders — without
 * that, scroll-spy triggers re-renders that hand mermaid fresh ids, mermaid
 * re-renders concurrently into the same DOM nodes, and the page flickers.
 */
function stableId(src: string, prefix: string): string {
  let h = 5381;
  for (let i = 0; i < src.length; i++) {
    h = ((h << 5) + h + src.charCodeAt(i)) | 0;
  }
  return `${prefix}-${(h >>> 0).toString(36)}`;
}

export interface MarkdownComponentOptions {
  onNavigatePage: (pageId: string) => void;
  onZoomDiagram?: (id: string) => void;
  /** Render ```mermaid fences as live diagrams (wiki pages only). */
  enableMermaid?: boolean;
}

/**
 * Inline citation chip — accent-tinted, monospace, `path:line`. Clicking it
 * scrolls to the matching {@link SourceCard} and flashes it. The card is
 * located by the shared `CitationRef.domId`, so this works whenever a card
 * for the citation exists (Q&A) and is a harmless no-op when none does
 * (wiki pages, which render the same chip statically).
 */
export function SrcChip({ citation }: { citation: Citation }) {
  const domId = CitationRef.domId(citation);
  const label = CitationRef.label(citation);

  const onClick = () => {
    const el = document.getElementById(domId);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    el.classList.add("src-card-flash");
    window.setTimeout(() => el.classList.remove("src-card-flash"), 1200);
  };

  return (
    <button
      type="button"
      onClick={onClick}
      title={citation.raw}
      className="inline-flex items-center gap-1 px-1.5 py-px rounded font-mono text-[11px] align-baseline cursor-pointer border-0 bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary))] hover:bg-[hsl(var(--primary))]/20 transition-colors"
    >
      <Github className="h-2.5 w-2.5" />
      <span className="truncate max-w-[260px]">{label}</span>
    </button>
  );
}

/**
 * Build the shared component map. Memoise at the call site (it depends only
 * on the option callbacks) so react-markdown doesn't re-walk the AST on
 * every parent re-render.
 */
export function buildMarkdownComponents(opts: MarkdownComponentOptions): Components {
  const { onNavigatePage, onZoomDiagram, enableMermaid } = opts;
  return {
    // ── Code: mermaid fences, fenced blocks, inline code ───────────────
    code({ className, children, ...rest }) {
      const lang = /language-(\w+)/.exec(className ?? "")?.[1];
      const text = String(children ?? "").replace(/\n$/, "");
      if (enableMermaid && lang === "mermaid") {
        const id = stableId(text, "wiki-d");
        return (
          <MermaidBlock
            key={id}
            diagramId={id}
            inlineSource={text}
            onZoom={() => onZoomDiagram?.(id)}
          />
        );
      }
      const node = (rest as { node?: { position?: { start: { line: number }; end: { line: number } } } }).node;
      const isBlock = Boolean(className) || node?.position?.start.line !== node?.position?.end.line;
      if (isBlock) {
        return (
          <pre className="my-4 overflow-x-auto rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] p-3 text-[12.5px] font-mono leading-[1.55] [&_code.hljs]:bg-transparent [&_code.hljs]:p-0">
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

    // ── Links: src: chips, relative internal page links, external ──────
    a({ href, children, ...rest }) {
      if (href?.startsWith("src:")) {
        const stripped = href.slice(4);
        const [path, range] = stripped.split("#");
        return <SrcChip citation={CitationRef.fromSrc(path, range)} />;
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

    // ── Typography ─────────────────────────────────────────────────────
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
  };
}
