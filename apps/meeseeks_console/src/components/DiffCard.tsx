import { useState, useMemo } from 'react';
import { CheckCircle2, XCircle, ChevronRight } from 'lucide-react';
import { CopyButton } from './CopyButton';
import { DiffStats } from './DiffStats';
import { parseDiffHunks } from '../utils/diff';
import { fileIconClass, basename, lineStyles } from '../utils/diffCard';
import { ParsedDiffFile, ParsedLine } from '../types';

// ── Always-dark backgrounds (matches TerminalCard — never changes with theme) ──
const TITLE_BG = 'bg-[hsl(220_5%_12%)]';  // ~#1c1d1e — dark chrome bar
const BODY_BG  = 'bg-[hsl(220_4%_10%)]';   // slightly lighter than terminal body

/** Max lines shown in collapsed preview per file section. */
const COLLAPSED_LINE_LIMIT = 8;

// ── Sub-components ──

export function HunkHeader({ header }: { header: string }) {
  return (
    <div className="px-3 py-0.5 bg-blue-500/[0.06] text-blue-400/70 text-[11px] select-none">
      {header}
    </div>
  );
}

export function DiffLine({ line, gutterWidth }: { line: ParsedLine; gutterWidth: number }) {
  const { bg, text, prefix } = lineStyles(line.type);
  const padW = `${gutterWidth}ch`;
  return (
    <div className={`flex ${bg} hover:brightness-110 transition-[filter]`}>
      {/* Old line number */}
      <span
        className="shrink-0 text-right text-[11px] text-white/25 select-none border-r border-white/5 pr-1"
        style={{ width: padW }}
      >
        {line.oldNumber ?? ''}
      </span>
      {/* New line number */}
      <span
        className="shrink-0 text-right text-[11px] text-white/25 select-none border-r border-white/5 pr-1 mr-2"
        style={{ width: padW }}
      >
        {line.newNumber ?? ''}
      </span>
      {/* Prefix (+/-/space) */}
      <span className={`shrink-0 w-4 text-center select-none ${text}`}>
        {prefix}
      </span>
      {/* Content */}
      <span className={`flex-1 whitespace-pre ${text}`}>
        {line.content.replace(/^[+ -]/, '')}
      </span>
    </div>
  );
}

export function FileDiffSection({
  file,
  expanded,
  gutterWidth,
}: {
  file: ParsedDiffFile;
  expanded: boolean;
  gutterWidth: number;
}) {
  // Flatten all lines across hunks for preview slicing
  const allItems = useMemo(() => {
    const result: ({ kind: 'hunk'; header: string } | { kind: 'line'; line: ParsedLine })[] = [];
    for (const hunk of file.hunks) {
      result.push({ kind: 'hunk', header: hunk.header });
      for (const line of hunk.lines) {
        result.push({ kind: 'line', line });
      }
    }
    return result;
  }, [file.hunks]);

  const totalLines = allItems.filter(i => i.kind === 'line').length;
  const visibleItems = expanded ? allItems : allItems.slice(0, COLLAPSED_LINE_LIMIT + file.hunks.length > 0 ? COLLAPSED_LINE_LIMIT + 1 : COLLAPSED_LINE_LIMIT);
  const hiddenLines = expanded ? 0 : Math.max(0, totalLines - COLLAPSED_LINE_LIMIT);

  return (
    <div className="overflow-x-auto text-xs font-mono">
      {(expanded ? allItems : visibleItems).map((item, i) =>
        item.kind === 'hunk' ? (
          <HunkHeader key={`h-${i}`} header={item.header} />
        ) : (
          <DiffLine key={`l-${i}`} line={item.line} gutterWidth={gutterWidth} />
        )
      )}
      {!expanded && hiddenLines > 0 && (
        <div className="px-3 py-2 text-center text-[11px] text-white/30 select-none">
          ⋯ {hiddenLines} more line{hiddenLines !== 1 ? 's' : ''} changed · Click to expand
        </div>
      )}
    </div>
  );
}

// ── Main component ──

interface DiffCardProps {
  title?: string;
  diffText: string;
  success?: boolean;
  defaultExpanded?: boolean;
}

export function DiffCard({
  title,
  diffText,
  success = true,
  defaultExpanded = false,
}: DiffCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const files = useMemo(() => parseDiffHunks(diffText), [diffText]);

  if (files.length === 0) {
    // No parseable diff — fall back to raw text display
    return (
      <div className={`rounded-lg overflow-hidden font-mono border border-[hsl(var(--border))] border-l-[3px] border-l-agent-4 ${TITLE_BG}`}>
        <div className="flex items-center gap-2 px-3 py-1.5">
          <span className="text-[11px] text-white/50 flex-1 truncate">{title || 'Edit'}</span>
          {success ? (
            <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500/70 shrink-0" />
          ) : (
            <XCircle className="w-3.5 h-3.5 text-red-400 shrink-0" />
          )}
        </div>
        <div className={`${BODY_BG} px-3 py-2`}>
          <pre className="text-xs text-white/60 whitespace-pre-wrap">{diffText || 'No changes'}</pre>
        </div>
      </div>
    );
  }

  // Compute gutter width from max line number across all files
  const maxLineNum = files.reduce((max, f) => {
    for (const h of f.hunks) {
      for (const l of h.lines) {
        if (l.oldNumber && l.oldNumber > max) max = l.oldNumber;
        if (l.newNumber && l.newNumber > max) max = l.newNumber;
      }
    }
    return max;
  }, 0);
  const gutterWidth = Math.max(3, String(maxLineNum).length + 1);

  // Determine if there's content worth expanding (more than COLLAPSED_LINE_LIMIT lines total)
  const totalLines = files.reduce(
    (sum, f) => sum + f.hunks.reduce((s, h) => s + h.lines.length, 0),
    0
  );
  const hasExpandableContent = totalLines > COLLAPSED_LINE_LIMIT;

  return (
    <div
      className={`rounded-lg overflow-hidden font-mono border border-[hsl(var(--border))] border-l-[3px] transition-colors ${
        hasExpandableContent ? 'cursor-pointer' : ''
      } ${success ? 'border-l-agent-4' : 'border-l-red-500'}`}
      onClick={() => hasExpandableContent && setExpanded(p => !p)}
    >
      {files.map((file, fi) => (
        <div key={`${file.path}-${fi}`}>
          {/* File header bar */}
          <div className={`flex items-center gap-2 px-3 py-1.5 ${TITLE_BG} ${fi > 0 ? 'border-t border-white/5' : ''}`}>
            {/* File icon */}
            <span className={`${fileIconClass(file.path)} shrink-0`} style={{ fontSize: '16px' }} />

            {/* Filename (basename visible, full path on hover) */}
            <span className="text-[11px] text-white/80 truncate flex-1 min-w-0" title={file.path}>
              {basename(file.path)}
            </span>

            {/* Diff stats */}
            <DiffStats additions={file.additions} deletions={file.deletions} className="shrink-0" />

            {/* Status indicator */}
            <span className="shrink-0">
              {success ? (
                <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500/70" />
              ) : (
                <XCircle className="w-3.5 h-3.5 text-red-400" />
              )}
            </span>

            {/* Expand chevron */}
            {hasExpandableContent && (
              <ChevronRight
                className={`w-3 h-3 shrink-0 text-white/25 transition-transform ${expanded ? 'rotate-90' : ''}`}
              />
            )}

            {/* Copy button (show on hover) */}
            <div className="group/copy relative">
              <CopyButton
                text={diffText}
                className="p-1 rounded text-white/40 hover:text-white/80 transition-all opacity-50 group-hover/copy:opacity-100 focus:opacity-100"
              />
            </div>
          </div>

          {/* Diff body */}
          <div className={`${BODY_BG} ${expanded ? 'max-h-[400px] overflow-y-auto' : ''}`}>
            <FileDiffSection file={file} expanded={expanded} gutterWidth={gutterWidth} />
          </div>
        </div>
      ))}

      {/* Collapse footer when expanded */}
      {expanded && hasExpandableContent && (
        <div className={`${BODY_BG} border-t border-white/5 px-3 py-1.5 text-center`}>
          <span className="text-[11px] text-white/30 select-none">Collapse ▲</span>
        </div>
      )}
    </div>
  );
}
