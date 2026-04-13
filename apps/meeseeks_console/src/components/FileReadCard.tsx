import { useMemo } from 'react';
import { FileText, Eye } from 'lucide-react';
import { LogEventCard } from './LogEventCard';
import { CopyButton } from './CopyButton';
import { ModelLabel } from './ModelLabel';
import { fileIconClass, basename } from '../utils/diffCard';
import { highlight, languageForExtension } from '../utils/highlight';

interface FileReadCardProps {
  path: string;
  // The text payload as emitted by the backend Read tool: each line prefixed
  // with `"{lineNum}\t"`, optionally followed by a truncation footer line
  // (`"... (truncated …)"`).
  text: string;
  totalLines?: number;
  timestamp?: string;
  model?: string;
  agentId?: string;
}

interface ParsedFile {
  startLine: number;
  endLine: number;
  visibleLines: number;
  rawContent: string;        // joined source without leading line numbers
  truncated: boolean;
  truncationNote?: string;
}

const TRUNCATION_PATTERNS = [
  /^\.\.\.\s*\(truncated.*\)\s*$/i,
];

/**
 * The backend emits each line as `"{n}\t{content}"` (1-based) with an
 * optional trailing `"... (truncated …)"` footer. Parse it back into a
 * gutter (line numbers) and a clean source body for syntax highlighting.
 */
function parseNumberedText(text: string): ParsedFile {
  if (!text) {
    return { startLine: 1, endLine: 0, visibleLines: 0, rawContent: '', truncated: false };
  }
  const lines = text.split('\n');
  let truncated = false;
  let truncationNote: string | undefined;

  // Detect and strip a final truncation footer line.
  const last = lines[lines.length - 1] ?? '';
  if (TRUNCATION_PATTERNS.some((re) => re.test(last))) {
    truncated = true;
    truncationNote = last.trim();
    lines.pop();
  }

  const lineNums: number[] = [];
  const contentLines: string[] = [];
  for (const line of lines) {
    const tab = line.indexOf('\t');
    if (tab > 0) {
      const numStr = line.slice(0, tab);
      const n = Number(numStr);
      if (Number.isFinite(n)) {
        lineNums.push(n);
        contentLines.push(line.slice(tab + 1));
        continue;
      }
    }
    // Non-numbered line (defensive: keep but use sequential gutter).
    lineNums.push(lineNums.length ? lineNums[lineNums.length - 1] + 1 : 1);
    contentLines.push(line);
  }

  return {
    startLine: lineNums[0] ?? 1,
    endLine: lineNums[lineNums.length - 1] ?? 0,
    visibleLines: lineNums.length,
    rawContent: contentLines.join('\n'),
    truncated,
    truncationNote,
  };
}

const AGENT_ID_TAG_CLASS =
  'text-[10px] font-mono px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))]';
const MODEL_TAG_CLASS =
  'text-[10px] font-mono text-[hsl(var(--muted-foreground))] bg-[hsl(var(--muted))] px-1.5 py-0.5 rounded whitespace-nowrap';

/**
 * Render a single line as a row in the viewer: line-number gutter + highlighted
 * content. `gutterWidth` is in characters so all rows align.
 */
function ViewerRow({
  lineNumber,
  html,
  gutterWidth,
}: {
  lineNumber: number;
  html: string;
  gutterWidth: number;
}) {
  return (
    <div className="flex hover:bg-[hsl(var(--code-border))]/50 transition-colors">
      <span
        className="shrink-0 text-right text-[11px] text-[hsl(var(--code-fg-subtle))] select-none border-r border-[hsl(var(--code-border))] pr-2 mr-2 leading-relaxed"
        style={{ width: `${gutterWidth}ch` }}
      >
        {lineNumber}
      </span>
      <code
        className="hljs flex-1 min-w-0 whitespace-pre-wrap break-all leading-relaxed text-[hsl(var(--code-fg))] bg-transparent"
        dangerouslySetInnerHTML={{ __html: html }}
      />
    </div>
  );
}

export function FileReadCard({
  path,
  text,
  totalLines,
  timestamp,
  model,
  agentId,
}: FileReadCardProps) {
  const parsed = useMemo(() => parseNumberedText(text), [text]);
  const language = useMemo(() => languageForExtension(path), [path]);

  // Highlight the whole body once, then split — keeps multi-line constructs
  // (string literals, block comments) parsing correctly.
  const lineHtml = useMemo(() => {
    if (!language) {
      // No highlighter for this extension — escape per line.
      return parsed.rawContent.split('\n').map((l) =>
        l.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      );
    }
    const fullHtml = highlight(parsed.rawContent, language).__html;
    return fullHtml.split('\n');
  }, [parsed.rawContent, language]);

  const gutterWidth = Math.max(2, String(parsed.endLine || parsed.visibleLines).length);
  const file = basename(path);
  const dir = path.slice(0, path.length - file.length).replace(/\/$/, '');

  // Range descriptor for the badge: "120 lines" if reading whole file,
  // "L1–L120 of 500" if a window of a longer file.
  const showsWhole = !totalLines || totalLines === parsed.visibleLines;
  const rangeLabel = showsWhole
    ? `${parsed.visibleLines} line${parsed.visibleLines === 1 ? '' : 's'}`
    : `L${parsed.startLine}–L${parsed.endLine} of ${totalLines}`;

  return (
    <LogEventCard
      icon={<Eye className="w-4 h-4 text-[hsl(var(--muted-foreground))]" />}
      title={
        <span className="flex items-center gap-2 min-w-0">
          {/* File-extension icon (vivid icons CDN — same as DiffCard/ReviewPane) */}
          <span className={`${fileIconClass(file)} shrink-0`} style={{ fontSize: '14px' }} />
          {/* Dir breadcrumb (truncates first) + filename (always visible) */}
          {dir && (
            <span
              className="text-[11px] text-[hsl(var(--muted-foreground))] font-mono truncate min-w-0"
              title={path}
            >
              {dir}/
            </span>
          )}
          <span className="text-sm font-mono text-[hsl(var(--foreground))] shrink-0" title={path}>
            {file}
          </span>
          {model && <ModelLabel modelId={model} className={MODEL_TAG_CLASS} />}
          {agentId && <span className={AGENT_ID_TAG_CLASS}>{agentId.slice(0, 8)}</span>}
        </span>
      }
      badge={
        <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full border whitespace-nowrap border-[hsl(var(--agent-1)/0.3)] bg-[hsl(var(--agent-1)/0.1)] text-[hsl(var(--agent-1))]">
          {rangeLabel}
          {parsed.truncated && ' · truncated'}
        </span>
      }
      timestamp={timestamp}
      accent="agent-1"
    >
      {/* Expanded body — file viewer with line gutter and syntax highlighting */}
      <div className="rounded-md overflow-hidden border border-[hsl(var(--code-border))] bg-[hsl(var(--code-body))] -mx-1">
        {/* Mini chrome bar with full path + copy */}
        <div className="group/copy relative flex items-center gap-2 px-3 py-1.5 bg-[hsl(var(--code-chrome))] border-b border-[hsl(var(--code-border))]">
          <FileText className="w-3 h-3 text-[hsl(var(--code-fg-muted))] shrink-0" />
          <span
            className="text-[11px] font-mono text-[hsl(var(--code-fg-muted))] truncate flex-1 min-w-0"
            title={path}
          >
            {path}
          </span>
          <CopyButton
            text={parsed.rawContent}
            className="p-1 rounded text-[hsl(var(--code-fg-subtle))] hover:text-[hsl(var(--code-fg))] transition-all opacity-50 group-hover/copy:opacity-100 focus:opacity-100"
          />
        </div>

        {/* Content viewer */}
        <div className="font-mono text-xs max-h-[500px] overflow-auto py-1">
          {parsed.visibleLines === 0 ? (
            <div className="px-3 py-3 text-[hsl(var(--code-fg-muted))]">
              (empty file)
            </div>
          ) : (
            lineHtml.map((html, i) => (
              <ViewerRow
                key={i}
                lineNumber={parsed.startLine + i}
                html={html}
                gutterWidth={gutterWidth}
              />
            ))
          )}

          {parsed.truncated && (
            <div className="px-3 pt-2 mt-1 border-t border-[hsl(var(--code-border))] text-[11px] text-[hsl(var(--code-fg-muted))] italic">
              {parsed.truncationNote ?? '… truncated'}
              {totalLines && ` · ${Math.max(0, totalLines - parsed.endLine)} more line(s) available`}
            </div>
          )}
        </div>
      </div>
    </LogEventCard>
  );
}
