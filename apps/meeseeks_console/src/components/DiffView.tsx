import React from 'react';
export function DiffView({
  diffContent = '',
  filename



}: {diffContent?: string;filename?: string;}) {
  const headerLabel = filename || 'Untitled';
  const lines = diffContent ? diffContent.split('\n') : [];
  return (
    <div className="h-full overflow-y-auto bg-[hsl(var(--background))] p-4 font-mono text-xs">
      <div className="rounded-lg border border-[hsl(var(--border))] overflow-hidden shadow-sm">
        <div className="bg-[hsl(var(--muted))] px-4 py-2 border-b border-[hsl(var(--border))] flex items-center justify-between">
          <span className="text-[hsl(var(--foreground))] font-medium">
            {headerLabel}
          </span>
          <span className="text-[hsl(var(--muted-foreground))]">Diff</span>
        </div>
        <div className="p-4 bg-[hsl(var(--card))] overflow-x-auto">
          {lines.map((line, i) => {
            let bgClass = '';
            let textClass = 'text-[hsl(var(--muted-foreground))]';
            if (line.startsWith('+')) {
              bgClass = 'bg-[hsl(var(--diff-add-bg))]';
              textClass = 'text-[hsl(var(--diff-add-text))]';
            } else if (line.startsWith('-')) {
              bgClass = 'bg-[hsl(var(--diff-del-bg))]';
              textClass = 'text-[hsl(var(--diff-del-text))]';
            } else if (line.startsWith('@@')) {
              textClass = 'text-[hsl(var(--diff-hunk-text))]';
            }
            return (
              <div
                key={i}
                className={`${bgClass} w-full px-2 py-0.5 whitespace-pre rounded-sm`}>

                <span className={`${textClass} font-medium`}>{line}</span>
              </div>);

          })}
        </div>
      </div>
    </div>);

}