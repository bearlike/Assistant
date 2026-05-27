/**
 * FieldHelp — the ONLY description renderer for the Settings UI.
 *
 * Replaces the two prior help styles (12px muted FieldTemplate text + 16px
 * full-foreground RJSF DescriptionField). One atom, one size (`helpCls`), one
 * markdown components map. Renders compact, inline-scoped markdown via
 * `react-markdown` + `remark-gfm`.
 *
 * Long help is collapsed: when the text spans multiple lines or runs past
 * ~140 chars, only the first line renders inline followed by a small `?`
 * trigger that opens a shadcn `<Popover>` showing the full markdown. The
 * popover (Radix) owns click-outside / Escape / focus — no hand-rolled state.
 */
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { HelpCircle } from "lucide-react";

import { Button } from "../../ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "../../ui/popover";
import { helpCls } from "../styles";

/** Anything longer than this (or multi-line) collapses to first-line + popover. */
const COLLAPSE_AT = 140;

/**
 * Module-level components map (never recreated per render — see console
 * CLAUDE.md). Compact, inline-ish: no large block margins, all colors via CSS
 * variables.
 */
const mdComponents: Components = {
  p: ({ children, ...rest }) => (
    <p {...rest} className="m-0 inline">
      {children}
    </p>
  ),
  code: ({ children, ...rest }) => (
    <code
      {...rest}
      className="font-mono text-[0.9em] bg-[hsl(var(--muted))]/40 rounded px-1"
    >
      {children}
    </code>
  ),
  a: ({ children, href, ...rest }) => (
    <a
      {...rest}
      href={href}
      target="_blank"
      rel="noreferrer"
      className="underline underline-offset-2 text-[hsl(var(--primary))]"
    >
      {children}
    </a>
  ),
  ul: ({ children, ...rest }) => (
    <ul {...rest} className="ml-4 list-disc space-y-0.5">
      {children}
    </ul>
  ),
  ol: ({ children, ...rest }) => (
    <ol {...rest} className="ml-4 list-decimal space-y-0.5">
      {children}
    </ol>
  ),
  li: ({ children, ...rest }) => <li {...rest}>{children}</li>,
  strong: ({ children, ...rest }) => (
    <strong {...rest} className="font-semibold text-[hsl(var(--foreground))]">
      {children}
    </strong>
  ),
  em: ({ children, ...rest }) => <em {...rest}>{children}</em>,
};

function Markdown({ text }: { text: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
      {text}
    </ReactMarkdown>
  );
}

/** Inline row: first-line help + the `?` trigger, top-aligned. */
const helpRowCls = "flex items-start gap-1";

interface FieldHelpProps {
  text?: string;
  id?: string;
}

export function FieldHelp({ text, id }: FieldHelpProps) {
  const trimmed = text?.trim();
  if (!trimmed) return null;

  const firstLine = trimmed.split("\n")[0];
  const collapse = trimmed.includes("\n") || trimmed.length > COLLAPSE_AT;

  if (!collapse) {
    return (
      <div id={id} className={helpCls}>
        <Markdown text={trimmed} />
      </div>
    );
  }

  return (
    <div id={id} className={helpRowCls}>
      <span className={helpCls}>
        <Markdown text={firstLine} />
      </span>
      <Popover>
        <PopoverTrigger asChild>
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            aria-label="More info"
            title="More info"
            className="h-4 w-4 shrink-0 text-[hsl(var(--muted-foreground))]"
          >
            <HelpCircle className="h-3 w-3" />
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className={`w-72 ${helpCls}`}>
          <Markdown text={trimmed} />
        </PopoverContent>
      </Popover>
    </div>
  );
}
