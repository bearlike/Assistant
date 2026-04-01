import { Copy } from 'lucide-react';
interface ShellBlockProps {
  content: string;
  title?: string;
  input?: string;
  output?: string;
}
export function ShellBlock({ content, title = 'shell', input, output }: ShellBlockProps) {
  const hasSections = input || output;
  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden my-4">
      <div className="flex items-center justify-between px-3 py-1.5 bg-[hsl(var(--muted))]/50 border-b border-[hsl(var(--border))]">
        <span className="text-xs font-medium text-[hsl(var(--muted-foreground))]">
          {title}
        </span>
        <button className="text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] transition-colors">
          <Copy className="w-3 h-3" />
        </button>
      </div>
      {hasSections ? (
        <>
          {input && (
            <div className="p-3 bg-[hsl(var(--muted))]/20">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Input</div>
              <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80">
                {input}
              </pre>
            </div>
          )}
          {input && output && (
            <div className="border-b border-[hsl(var(--border))]" />
          )}
          {output && (
            <div className="p-3">
              <div className="text-[10px] font-medium text-[hsl(var(--muted-foreground))] uppercase tracking-wider mb-1">Output</div>
              <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80">
                {output}
              </pre>
            </div>
          )}
        </>
      ) : (
        <div className="p-3 overflow-x-auto">
          <pre className="text-xs font-mono text-[hsl(var(--foreground))] whitespace-pre-wrap leading-relaxed opacity-80">
            {content}
          </pre>
        </div>
      )}
    </div>
  );
}
