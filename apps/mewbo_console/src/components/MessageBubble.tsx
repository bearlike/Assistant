import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import rehypeHighlight from 'rehype-highlight';
// hljs theme: theme-aware via --hl-* CSS variables in src/index.css
// (no third-party CSS import — adapts to light/dark mode automatically).
import { ChevronDown, ChevronUp } from 'lucide-react';
import { CopyButton } from './CopyButton';
interface MessageBubbleProps {
  role: 'user' | 'system' | 'ai' | 'assistant';
  content?: string;
  actions?: React.ReactNode;
  children?: React.ReactNode;
}
const USER_COLLAPSE_THRESHOLD = 300;
export function MessageBubble({ role, content, actions, children }: MessageBubbleProps) {
  const [expanded, setExpanded] = useState(false);
  const markdown = content ? <MarkdownContent content={content} /> : null;
  if (role === 'user') {
    const safeContent = content ?? '';
    const isLong = safeContent.length > USER_COLLAPSE_THRESHOLD;
    const displayContent =
    isLong && !expanded ?
    safeContent.slice(0, USER_COLLAPSE_THRESHOLD).trimEnd() + '…' :
    safeContent;
    return (
      <div className="flex justify-end">
        <div className="flex flex-col items-end">
          <div className="bg-user-msg hover:bg-user-msg-hover text-[hsl(var(--card-foreground))] px-4 py-3 bubble-notch text-sm border border-[hsl(var(--border))] hover:border-[hsl(var(--border-strong))] transition-colors break-words">
            {displayContent ? <MarkdownContent content={displayContent} /> : null}
            {isLong &&
            <>
              <div className="border-t border-[hsl(var(--border))] mt-2 pt-1.5" />
              <button
                onClick={() => setExpanded(!expanded)}
                className="flex items-center gap-1.5 text-xs font-medium text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                  {expanded ?
                <>
                      Show less <ChevronUp className="w-3.5 h-3.5" />
                    </> :

                <>
                      Show more
                      <span className="font-normal opacity-70">
                        (+{safeContent.length - USER_COLLAPSE_THRESHOLD} chars)
                      </span>
                      <ChevronDown className="w-3.5 h-3.5" />
                    </>
                }
              </button>
            </>
            }
          </div>
        </div>
      </div>);

  }
  if (role === 'assistant' || role === 'ai') {
    return (
      <div className="flex flex-col w-full">
        {content &&
        <div className="text-[hsl(var(--foreground))] text-sm">{markdown}</div>
        }
        {content && (
          <div className="mt-2 inline-flex items-center gap-3">
            <CopyButton
              text={content}
              className="group inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors"
            >
              <span className="hidden text-[10px] group-hover:inline-block">Copy</span>
            </CopyButton>
            {actions}
          </div>
        )}
        {children}
      </div>);

  }
  return (
    <div className="flex flex-col w-full">
      {content &&
      <div className="text-[hsl(var(--foreground))] text-sm">{markdown}</div>
      }
      {children}
    </div>);

}
const markdownComponents: React.ComponentProps<typeof ReactMarkdown>['components'] = {
  p: ({ ...props }) =>
    <p className="mb-2 last:mb-0 leading-relaxed" {...props} />,

  a: ({ ...props }) =>
    <a
      className="text-[hsl(var(--primary))] underline underline-offset-2 hover:opacity-80"
      {...props} />,

  ul: ({ ...props }) =>
    <ul className="list-disc pl-5 space-y-1.5 mb-2 last:mb-0 mt-2" {...props} />,

  ol: ({ ...props }) =>
    <ol className="list-decimal pl-5 space-y-1.5 mb-2 last:mb-0 mt-2" {...props} />,

  li: ({ ...props }) =>
    <li className="leading-relaxed" {...props} />,

  blockquote: ({ ...props }) =>
    <blockquote
      className="my-3.5 px-4 py-3 border-y border-[hsl(var(--border))] bg-[hsl(var(--muted))]/45 text-[hsl(var(--foreground))]/90 not-italic [&>p]:my-0 [&>p+p]:mt-1.5 [&>p>strong]:font-semibold [&>p>strong]:text-[hsl(var(--foreground))] [&>p>code]:bg-[hsl(var(--background))]/60"
      {...props} />,

  // Inline code only — block code inside <pre> is handled by rehype-highlight
  code: ({ className, ...props }) => {
    if (className?.startsWith('hljs') || className?.startsWith('language-')) {
      return <code className={className} {...props} />;
    }
    return (
      <code
        className="rounded bg-[hsl(var(--muted))] px-1 py-0.5 text-xs text-[hsl(var(--foreground))]"
        {...props} />
    );
  },

  pre: ({ ...props }) =>
    <pre
      className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 overflow-x-auto text-xs my-2 [&_code.hljs]:bg-transparent [&_code.hljs]:p-0"
      {...props} />,

  // Headings
  h1: ({ ...props }) =>
    <h1 className="text-lg font-bold text-[hsl(var(--foreground))] mt-4 mb-2" {...props} />,

  h2: ({ ...props }) =>
    <h2 className="text-base font-semibold text-[hsl(var(--foreground))] mt-3.5 mb-1.5" {...props} />,

  h3: ({ ...props }) =>
    <h3 className="text-sm font-semibold text-[hsl(var(--foreground))] mt-3 mb-1.5" {...props} />,

  h4: ({ ...props }) =>
    <h4 className="text-sm font-medium text-[hsl(var(--foreground))] mt-2 mb-1" {...props} />,

  h5: ({ ...props }) =>
    <h5 className="text-xs font-semibold text-[hsl(var(--foreground))] mt-2 mb-1" {...props} />,

  h6: ({ ...props }) =>
    <h6 className="text-xs font-medium text-[hsl(var(--muted-foreground))] mt-2 mb-1" {...props} />,

  hr: () =>
    <hr className="border-[hsl(var(--border))] my-4" />,

  // Tables
  table: ({ ...props }) =>
    <div className="overflow-x-auto my-3 rounded-lg border border-[hsl(var(--border))]">
      <table className="w-full text-sm" {...props} />
    </div>,

  thead: ({ ...props }) =>
    <thead className="bg-[hsl(var(--muted))]" {...props} />,

  tbody: ({ ...props }) =>
    <tbody {...props} />,

  tr: ({ ...props }) =>
    <tr className="border-b border-[hsl(var(--border))] last:border-b-0 even:bg-[hsl(var(--muted))]/50" {...props} />,

  th: ({ ...props }) =>
    <th className="text-left font-semibold px-3 py-2 text-[hsl(var(--foreground))]" {...props} />,

  td: ({ ...props }) =>
    <td className="px-3 py-2 text-[hsl(var(--foreground))]" {...props} />,
};

export function MarkdownContent({ content }: {content: string;}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[rehypeHighlight]}
      components={markdownComponents}>
      {content}
    </ReactMarkdown>
  );
}
