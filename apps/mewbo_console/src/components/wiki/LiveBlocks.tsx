/**
 * Renders the streaming Q&A answer `Block[]`. Blocks grow over time as
 * `useQaStream` folds in `block_delta` events, so the "typewriter" effect is
 * produced by the stream itself — this component just paints.
 *
 * DRY: prose blocks route through the SAME react-markdown pipeline the wiki
 * page renderer uses (`buildMarkdownComponents` + remarkGfm + rehypeHighlight
 * + rehypeSlug). That gives the answer full markdown fidelity — H2/H3, nested
 * + ordered lists, inline code, fenced code blocks (highlighted), tables,
 * blockquotes, and `src:` citation chips — with one renderer, not two.
 *
 * The terminal `sources` block is NOT rendered here: QAScreen extracts it to
 * drive the right-panel source cards. LiveBlocks paints prose only. Mermaid
 * is off (Q&A answers never contain diagrams).
 *
 * String text inside a block is parsed as markdown. The playbook asks the QA
 * agent to emit structured inline nodes ({"code": …}, {"link": …}), but LLMs
 * reliably write plain markdown syntax (`**bold**`, backticks, links) inside
 * the string instead. Both paths land on the same shared chip/link visuals.
 */

import { useMemo } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import rehypeSlug from "rehype-slug";

import { CitationRef } from "./citations";
import { SrcChip, buildMarkdownComponents } from "./markdownComponents";
import type { Block, InlineNode } from "./api/types";

interface LiveBlocksProps {
  blocks: Block[];
  onNavigatePage: (pageId: string) => void;
}

export function LiveBlocks({ blocks, onNavigatePage }: LiveBlocksProps) {
  const components = useMemo(
    () => buildMarkdownComponents({ onNavigatePage, enableMermaid: false }),
    [onNavigatePage],
  );
  return (
    <>
      {blocks.map((block, i) => (
        <BlockView key={i} block={block} components={components} onNavigatePage={onNavigatePage} />
      ))}
    </>
  );
}

/** A markdown string → the shared full renderer (block-level container). */
function Prose({ source, components }: { source: string; components: Components }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight, rehypeSlug]}
      components={components}
    >
      {source}
    </ReactMarkdown>
  );
}

function BlockView({
  block,
  components,
  onNavigatePage,
}: {
  block: Block;
  components: Components;
  onNavigatePage: (pageId: string) => void;
}): React.ReactNode {
  switch (block.kind) {
    case "p":
      return <ParagraphBlock node={block.text} components={components} onNavigatePage={onNavigatePage} />;
    case "h2":
      return <Prose source={`## ${block.text}`} components={components} />;
    case "h3":
      return <Prose source={`### ${block.text}`} components={components} />;
    case "hr":
      return <Prose source={"---"} components={components} />;
    case "ul":
      return <ListBlock items={block.items} components={components} onNavigatePage={onNavigatePage} />;
    case "table":
      return <TableBlock head={block.head} rows={block.rows} components={components} onNavigatePage={onNavigatePage} />;
    case "diagram":
      // Diagrams aren't valid inside QA answers and never stream here.
      return null;
    case "accordion":
    case "sources":
      // The terminal ``sources`` block (and any accordion) feeds the right
      // panel via QAScreen — never rendered inline in the prose column.
      return null;
    default:
      return null;
  }
}

/**
 * A paragraph whose text is either a plain markdown string (common case →
 * full renderer) or a structured array of inline atoms (rare → atom path,
 * still routed to the shared chip/link visuals).
 */
function ParagraphBlock({
  node,
  components,
  onNavigatePage,
}: {
  node: InlineNode;
  components: Components;
  onNavigatePage: (pageId: string) => void;
}) {
  if (typeof node === "string") {
    return <Prose source={node} components={components} />;
  }
  return (
    <p className="text-[14.5px] leading-[1.7] text-[hsl(var(--foreground))] [text-wrap:pretty] my-4 first:mt-0">
      {renderInlineAtoms(node, onNavigatePage)}
    </p>
  );
}

function ListBlock({
  items,
  components,
  onNavigatePage,
}: {
  items: InlineNode[];
  components: Components;
  onNavigatePage: (pageId: string) => void;
}) {
  return (
    <ul className="my-4 space-y-1.5 list-disc pl-5 marker:text-[hsl(var(--muted-foreground))] text-[14.5px] leading-[1.7]">
      {items.map((it, j) => (
        <li key={j} className="[text-wrap:pretty]">
          {typeof it === "string" ? (
            <Prose source={it} components={components} />
          ) : (
            renderInlineAtoms(it, onNavigatePage)
          )}
        </li>
      ))}
    </ul>
  );
}

function TableBlock({
  head,
  rows,
  components,
  onNavigatePage,
}: {
  head: string[];
  rows: InlineNode[][];
  components: Components;
  onNavigatePage: (pageId: string) => void;
}) {
  return (
    <div className="my-5 overflow-x-auto rounded-md border border-[hsl(var(--border))]">
      <table className="w-full text-sm">
        <thead className="bg-[hsl(var(--muted))]/50">
          <tr>
            {head.map((h, i) => (
              <th
                key={i}
                className="text-left font-medium text-xs uppercase tracking-wide text-[hsl(var(--muted-foreground))] px-3 py-2"
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, r) => (
            <tr key={r} className="border-t border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]/30">
              {row.map((cell, c) => (
                <td key={c} className="px-3 py-2.5 align-top [text-wrap:pretty]">
                  {typeof cell === "string" ? (
                    <Prose source={cell} components={components} />
                  ) : (
                    renderInlineAtoms(cell, onNavigatePage)
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Structured inline atoms ({code}/{link}/{src}). Plain strings inside an atom
 * array still render verbatim — the common markdown-in-string case is handled
 * by {@link Prose} at the block level above.
 */
function renderInlineAtoms(node: InlineNode, onNavigatePage: (p: string) => void): React.ReactNode {
  if (typeof node === "string") return node;
  if (Array.isArray(node)) {
    return node.map((n, i) => (
      <span key={i} className="contents">
        {renderInlineAtoms(n, onNavigatePage)}
      </span>
    ));
  }
  if (!node) return null;
  if ("code" in node) {
    return (
      <code className="font-mono text-[0.85em] px-1 py-0.5 rounded bg-[hsl(var(--muted))]/70 text-[hsl(var(--foreground))]">
        {node.code}
      </code>
    );
  }
  if ("link" in node) {
    return (
      <button
        type="button"
        onClick={() => onNavigatePage(node.link)}
        className="inline text-[hsl(var(--primary))] hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
      >
        {node.text}
      </button>
    );
  }
  if ("kind" in node && node.kind === "src") {
    return <SrcChip citation={CitationRef.fromSrc(node.path, node.lines)} />;
  }
  return null;
}
