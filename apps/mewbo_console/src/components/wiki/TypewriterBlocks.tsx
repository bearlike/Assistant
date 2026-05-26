/**
 * Typewriter renderer for the Q&A page. Walks the block array, flattens
 * each block into a character count, advances a global cursor at a bursty
 * tick rate, and re-renders partial blocks each frame.
 *
 * Inline-tree aware: source chips, internal links, and code chips reveal
 * mid-stream rather than popping in whole.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { Github } from "lucide-react";

import type { Block, InlineNode } from "./api/types";

interface InlineCtx {
  onNavigatePage: (pageId: string) => void;
}

// QA answers are still served as the typed Block model — the typewriter
// needs inline-tree-aware rendering so source chips, code chips, and
// internal links reveal as the cursor crosses them. This helper mirrors
// the markdown renderer's atoms; kept inline because TypewriterBlocks is
// the only consumer of the Block path now.
function renderInline(node: InlineNode, ctx: InlineCtx): React.ReactNode {
  if (typeof node === "string") return node;
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
        className="inline text-[hsl(var(--primary))] hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0"
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

interface TypewriterBlocksProps {
  blocks: Block[];
  onNavigatePage: (pageId: string) => void;
  /** Chars per tick. The render uses jitter ± so cadence feels human. */
  speed?: number;
}

export function TypewriterBlocks({
  blocks,
  onNavigatePage,
  speed = 12,
}: TypewriterBlocksProps) {
  const flat = useMemo(() => flattenBlocks(blocks), [blocks]);
  const total = useMemo(() => flat.reduce((sum, c) => sum + c, 0), [flat]);
  const [cursor, setCursor] = useState(0);
  const cursorRef = useRef(0);

  useEffect(() => {
    cursorRef.current = 0;
    setCursor(0);
    let cancelled = false;
    const tick = () => {
      if (cancelled) return;
      const step = Math.max(1, Math.round(speed * (0.8 + Math.random() * 0.6)));
      cursorRef.current = Math.min(total, cursorRef.current + step);
      setCursor(cursorRef.current);
      if (cursorRef.current < total) {
        window.setTimeout(tick, 16 + Math.random() * 18);
      }
    };
    const t = window.setTimeout(tick, 200);
    return () => {
      cancelled = true;
      window.clearTimeout(t);
    };
  }, [blocks, total, speed]);

  const done = cursor >= total;
  const ctx: InlineCtx = { onNavigatePage };

  let remaining = cursor;
  const out: React.ReactNode[] = [];
  for (let bi = 0; bi < blocks.length; bi++) {
    const block = blocks[bi];
    const len = flat[bi];
    if (remaining <= 0 && cursor < total) break;
    const portion = Math.min(len, remaining);
    out.push(renderPartialBlock(block, bi, portion, ctx));
    remaining = Math.max(0, remaining - portion);
  }

  return (
    <>
      {out}
      {!done && (
        <span
          aria-hidden="true"
          className="inline-block w-[2px] h-[1em] align-text-bottom -mb-px ml-px bg-[hsl(var(--primary))] animate-[wiki-caret_900ms_steps(2)_infinite]"
        />
      )}
    </>
  );
}

// ── flatten / truncate helpers ───────────────────────────────────────

function flattenBlocks(blocks: Block[]): number[] {
  return blocks.map(blockLength);
}

function blockLength(b: Block): number {
  switch (b.kind) {
    case "p":
      return inlineLength(b.text);
    case "h3":
    case "h2":
      return b.text.length;
    case "ul":
      // +1 per item for joiner cost so the cursor crosses items naturally
      return b.items.reduce((sum, it) => sum + inlineLength(it) + 1, 0);
    default:
      return 0;
  }
}

function inlineLength(node: InlineNode): number {
  if (typeof node === "string") return node.length;
  if (Array.isArray(node)) return node.reduce<number>((s, n) => s + inlineLength(n), 0);
  if (!node) return 0;
  if ("code" in node) return node.code.length;
  if ("link" in node) return node.text.length;
  if ("kind" in node && node.kind === "src") return node.path.length;
  return 0;
}

function renderPartialBlock(
  block: Block,
  key: number,
  limit: number,
  ctx: InlineCtx
): React.ReactNode {
  if (block.kind === "h3") {
    return (
      <h3 key={key} className="text-[16px] font-semibold tracking-tight mt-7 mb-2">
        {block.text.slice(0, limit)}
      </h3>
    );
  }
  if (block.kind === "h2") {
    return (
      <h2 key={key} className="text-[22px] font-semibold tracking-[-0.02em] mt-10 mb-3">
        {block.text.slice(0, limit)}
      </h2>
    );
  }
  if (block.kind === "p") {
    const total = inlineLength(block.text);
    return (
      <p
        key={key}
        className="text-[14.5px] leading-[1.7] text-[hsl(var(--foreground))] [text-wrap:pretty] my-4 first:mt-0"
      >
        {limit >= total
          ? renderInline(block.text, ctx)
          : renderTruncatedInline(block.text, limit, ctx)}
      </p>
    );
  }
  if (block.kind === "ul") {
    const items: React.ReactNode[] = [];
    let rem = limit;
    for (let i = 0; i < block.items.length; i++) {
      if (rem <= 0) break;
      const fl = inlineLength(block.items[i]);
      const take = Math.min(fl, rem);
      items.push(
        <li key={i} className="[text-wrap:pretty]">
          {take >= fl
            ? renderInline(block.items[i], ctx)
            : renderTruncatedInline(block.items[i], take, ctx)}
        </li>
      );
      rem -= take + 1;
    }
    return (
      <ul
        key={key}
        className="my-4 space-y-1.5 list-disc pl-5 marker:text-[hsl(var(--muted-foreground))] text-[14.5px] leading-[1.7]"
      >
        {items}
      </ul>
    );
  }
  return null;
}

function renderTruncatedInline(
  node: InlineNode,
  limit: number,
  ctx: InlineCtx
): React.ReactNode {
  if (typeof node === "string") return node.slice(0, limit);
  if (Array.isArray(node)) {
    const out: React.ReactNode[] = [];
    let rem = limit;
    for (let i = 0; i < node.length; i++) {
      if (rem <= 0) break;
      const fl = inlineLength(node[i]);
      if (fl <= rem) {
        out.push(
          <span key={i} className="contents">
            {renderInline(node[i], ctx)}
          </span>
        );
        rem -= fl;
      } else {
        out.push(
          <span key={i} className="contents">
            {renderTruncatedInline(node[i], rem, ctx)}
          </span>
        );
        rem = 0;
      }
    }
    return out;
  }
  if (!node) return null;
  if ("code" in node) {
    return (
      <code className="font-mono text-[0.85em] px-1 py-0.5 rounded bg-[hsl(var(--muted))]/70">
        {node.code.slice(0, limit)}
      </code>
    );
  }
  if ("link" in node) {
    return (
      <button
        type="button"
        onClick={() => ctx.onNavigatePage(node.link)}
        className="inline text-[hsl(var(--primary))] hover:underline underline-offset-2 cursor-pointer bg-transparent border-0 p-0"
      >
        {node.text.slice(0, limit)}
      </button>
    );
  }
  if ("kind" in node && node.kind === "src") {
    // Source chips render whole-or-nothing — they're visual atoms.
    return renderInline(node, ctx);
  }
  return null;
}
