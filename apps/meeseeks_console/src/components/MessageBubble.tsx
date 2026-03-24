import React, { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ChevronDown, ChevronUp, Copy } from 'lucide-react';
import { copyText } from '../utils/clipboard';
interface MessageBubbleProps {
  role: 'user' | 'system' | 'ai' | 'assistant';
  content?: string;
  children?: React.ReactNode;
}
const USER_COLLAPSE_THRESHOLD = 180;
export function MessageBubble({ role, content, children }: MessageBubbleProps) {
  const [expanded, setExpanded] = useState(false);
  const markdown = content ? <MarkdownContent content={content} /> : null;
  const copyContent = async () => {
    if (!content) {
      return;
    }
    await copyText(content);
  };
  const CopyButton = () =>
  <button
    onClick={copyContent}
    className="group mt-2 inline-flex items-center gap-1 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

      <Copy className="w-3 h-3" />
      <span className="hidden text-[10px] group-hover:inline-block">
        Copy
      </span>
    </button>;
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
          <div className="bg-user-msg hover:bg-user-msg-hover text-[hsl(var(--card-foreground))] px-4 py-3 rounded-lg max-w-[70%] text-sm border border-[hsl(var(--border))] transition-colors">
            {displayContent ? <MarkdownContent content={displayContent} /> : null}
            {isLong &&
            <button
              onClick={() => setExpanded(!expanded)}
              className="flex items-center gap-1 mt-2 text-xs text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">

                {expanded ?
              <>
                    Show less <ChevronUp className="w-3 h-3" />
                  </> :

              <>
                    Show more <ChevronDown className="w-3 h-3" />
                  </>
              }
              </button>
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
        {content && <CopyButton />}
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
function MarkdownContent({ content }: {content: string;}) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        p: ({ ...props }) =>
        <p className="mb-2 last:mb-0 leading-relaxed" {...props} />,

        a: ({ ...props }) =>
        <a
          className="text-[hsl(var(--primary))] underline underline-offset-2 hover:opacity-80"
          {...props} />,


        ul: ({ ...props }) =>
        <ul
          className="list-disc pl-5 space-y-1.5 mb-2 last:mb-0 mt-2"
          {...props} />,


        ol: ({ ...props }) =>
        <ol
          className="list-decimal pl-5 space-y-1.5 mb-2 last:mb-0 mt-2"
          {...props} />,


        li: ({ ...props }) => <li className="leading-relaxed" {...props} />,
        blockquote: ({ ...props }) =>
        <blockquote
          className="border-l-2 border-[hsl(var(--border))] pl-3 text-[hsl(var(--muted-foreground))] italic my-2"
          {...props} />,


        code: ({ ...props }) =>
        <code
          className="rounded bg-[hsl(var(--muted))] px-1 py-0.5 text-xs text-[hsl(var(--foreground))]"
          {...props} />,


        pre: ({ ...props }) =>
        <pre
          className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-3 overflow-x-auto text-xs my-2"
          {...props} />,


        h3: ({ ...props }) =>
        <h3
          className="text-sm font-semibold text-[hsl(var(--foreground))] mt-3 mb-1.5"
          {...props} />,


        h4: ({ ...props }) =>
        <h4
          className="text-sm font-medium text-[hsl(var(--foreground))] mt-2 mb-1"
          {...props} />


      }}>

      {content}
    </ReactMarkdown>);

}
