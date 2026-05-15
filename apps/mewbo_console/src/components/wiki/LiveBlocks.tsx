/**
 * Renders a `Block[]` exactly as it currently is. Blocks grow over time as
 * `useQaStream` folds in `block_delta` events, so the "typewriter" effect
 * is produced by the stream itself — this component just paints.
 *
 * Inline atom support mirrors the wiki page renderer (source chips, code
 * chips, internal page links). Mermaid diagrams aren't valid inside QA
 * answers, so they're intentionally not handled here.
 *
 * String text inside a block is parsed as markdown. The playbook asks the
 * QA agent to emit structured inline nodes ({"code": …}, {"link": …}), but
 * LLMs reliably write plain markdown syntax (`**bold**`, backticks, links)
 * inside the string instead. Parsing it here covers the common case
 * without changing the wire shape — typed inline atoms still take the
 * structured path below.
 */

import { Github } from "lucide-react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import type { Block, InlineNode } from "./api/types";

interface InlineCtx {
  onNavigatePage: (pageId: string) => void;
}

interface LiveBlocksProps {
  blocks: Block[];
  onNavigatePage: (pageId: string) => void;
}

// Module-level so react-markdown doesn't rebuild the map on every render.
// `p` flattens to a fragment so the parsed markdown stays inline inside
// our wrapping <p>/<li>. Code and link styles match the typed-atom
// renderers below so the two paths produce identical visuals.
const inlineMarkdown: Components = {
  p: ({ children }) => <>{children}</>,
  code: ({ children, ...rest }) => (
    <code
      {...rest}
      className="font-mono text-[0.85em] px-1 py-0.5 rounded bg-[hsl(var(--muted))]/70 text-[hsl(var(--foreground))]"
    >
      {children}
    </code>
  ),
  a: ({ href, children }) => (
    <a
      href={href}
      target={href?.startsWith("http") ? "_blank" : undefined}
      rel={href?.startsWith("http") ? "noopener noreferrer" : undefined}
      className="text-[hsl(var(--primary))] hover:underline underline-offset-2"
    >
      {children}
    </a>
  ),
};

export function LiveBlocks({ blocks, onNavigatePage }: LiveBlocksProps) {
  const ctx: InlineCtx = { onNavigatePage };
  return <>{blocks.map((block, i) => renderBlock(block, i, ctx))}</>;
}

function renderBlock(block: Block, key: number, ctx: InlineCtx): React.ReactNode {
  switch (block.kind) {
    case "p":
      return (
        <p
          key={key}
          className="text-[14.5px] leading-[1.7] text-[hsl(var(--foreground))] [text-wrap:pretty] my-4 first:mt-0"
        >
          {renderInline(block.text, ctx)}
        </p>
      );
    case "h2":
      return (
        <h2
          key={key}
          className="text-[22px] font-semibold tracking-[-0.02em] mt-10 mb-3"
        >
          {block.text}
        </h2>
      );
    case "h3":
      return (
        <h3
          key={key}
          className="text-[16px] font-semibold tracking-tight mt-7 mb-2"
        >
          {block.text}
        </h3>
      );
    case "ul":
      return (
        <ul
          key={key}
          className="my-4 space-y-1.5 list-disc pl-5 marker:text-[hsl(var(--muted-foreground))] text-[14.5px] leading-[1.7]"
        >
          {block.items.map((it, j) => (
            <li key={j} className="[text-wrap:pretty]">
              {renderInline(it, ctx)}
            </li>
          ))}
        </ul>
      );
    case "hr":
      return <hr key={key} className="my-6 border-0 border-t border-[hsl(var(--border))]" />;
    default:
      return null;
  }
}

function renderInline(node: InlineNode, ctx: InlineCtx): React.ReactNode {
  if (typeof node === "string") {
    return (
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={inlineMarkdown}>
        {node}
      </ReactMarkdown>
    );
  }
  if (Array.isArray(node)) {
    return node.map((n, i) => (
      <span key={i} className="contents">
        {renderInline(n, ctx)}
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
        onClick={() => ctx.onNavigatePage(node.link)}
        className="inline text-[hsl(var(--primary))] hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0 font-inherit"
      >
        {node.text}
      </button>
    );
  }
  if ("kind" in node && node.kind === "src") {
    return (
      <span
        className="inline-flex items-center gap-1 px-1.5 py-px rounded font-mono text-[11px] bg-[hsl(var(--muted))]/60 text-[hsl(var(--muted-foreground))] align-baseline"
        title={node.path}
      >
        <Github className="h-2.5 w-2.5" />
        <span className="truncate max-w-[260px]">{node.path}</span>
        {node.lines && (
          <span className="text-[10px] text-[hsl(var(--muted-foreground))]/80">{node.lines}</span>
        )}
      </span>
    );
  }
  return null;
}
