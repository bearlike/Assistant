import { useState } from 'react';
import { CheckCircle2, XCircle, ChevronRight } from 'lucide-react';
import { CopyButton } from './CopyButton';
import { ModelLabel } from './ModelLabel';
import { HighlightedCode } from './HighlightedCode';
import { formatDuration } from '../utils/time';

interface TerminalCardProps {
  command: string;
  cwd?: string;
  exitCode?: number;
  stdout?: string;
  stderr?: string;
  durationMs?: number;
  defaultExpanded?: boolean;
  model?: string;
  agentId?: string;
}

function shortenCwd(cwd: string): string {
  const parts = cwd.replace(/\/$/, '').split('/');
  if (parts.length <= 3) return cwd;
  const home = parts[0] === '' && parts[1] === 'home' ? 2 : 0;
  if (home && parts.length > home + 2) {
    return '~/' + parts.slice(-2).join('/');
  }
  return '.../' + parts.slice(-2).join('/');
}

function countLines(text: string): number {
  if (!text) return 0;
  return text.split('\n').length;
}

// Theme-aware code surfaces — see --code-chrome / --code-body in src/index.css.
// Both adapt automatically to light/dark mode.
const TITLE_BG = 'bg-[hsl(var(--code-chrome))]';
const BODY_BG = 'bg-[hsl(var(--code-body))]';

export function TerminalCard({
  command,
  cwd,
  exitCode,
  stdout,
  stderr,
  durationMs,
  defaultExpanded = false,
  model,
  agentId,
}: TerminalCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const isError = exitCode !== undefined && exitCode !== 0;
  const hasOutput = !!(stdout || stderr);
  const outputLines = countLines(stdout || '') + countLines(stderr || '');

  return (
    <div
      className={`rounded-lg overflow-hidden font-mono border border-[hsl(var(--border))] border-l-[3px] transition-colors ${
        hasOutput ? 'cursor-pointer' : ''
      } ${
        isError ? 'border-l-[hsl(var(--destructive))]' : 'border-l-[hsl(var(--success)/0.6)]'
      }`}
      onClick={() => hasOutput && setExpanded((p) => !p)}
    >
      {/* Title bar — terminal window chrome */}
      <div className={`flex items-center gap-2 px-3 py-1.5 ${TITLE_BG}`}>
        {/* Traffic-light dots */}
        <div className="flex items-center gap-1.5 shrink-0">
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-[hsl(var(--destructive))]' : 'bg-[hsl(var(--success))]'}`} />
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-[hsl(var(--destructive)/0.3)]' : 'bg-[hsl(var(--code-fg-subtle))]/30'}`} />
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-[hsl(var(--destructive)/0.3)]' : 'bg-[hsl(var(--code-fg-subtle))]/30'}`} />
        </div>

        {/* CWD tab title */}
        {cwd && (
          <span
            className="text-2xs text-[hsl(var(--code-fg-muted))] truncate flex-1 min-w-0"
            title={cwd}
          >
            {shortenCwd(cwd)}
          </span>
        )}
        {!cwd && <span className="flex-1" />}

        {/* Model + Agent badges */}
        {model && <ModelLabel modelId={model} className="font-sans text-2xs text-[hsl(var(--code-fg-muted))]" />}
        {agentId && (
          <span className="text-2xs font-mono text-[hsl(var(--code-fg-muted))] px-1 rounded bg-[hsl(var(--code-border))]">
            {agentId.slice(0, 6)}
          </span>
        )}

        {/* Duration */}
        {durationMs !== undefined && (
          <span className="font-sans text-2xs text-[hsl(var(--code-fg-subtle))] shrink-0 hidden sm:inline">
            {formatDuration(durationMs)}
          </span>
        )}

        {/* Status indicator — only shown when exit code is known */}
        {exitCode !== undefined && (
          <span className="shrink-0">
            {isError ? (
              <span className="flex items-center gap-1">
                <XCircle className="w-3.5 h-3.5 text-[hsl(var(--destructive))]" />
                <span className="text-2xs text-[hsl(var(--destructive-text))]">{exitCode}</span>
              </span>
            ) : (
              <CheckCircle2 className="w-3.5 h-3.5 text-[hsl(var(--success)/0.7)]" />
            )}
          </span>
        )}
      </div>

      {/* Terminal body */}
      <div className={BODY_BG}>
        {/* Command line — always visible, syntax-highlighted as bash */}
        <div className="group/copy relative px-3 py-2">
          <div className="flex items-center gap-2">
            <div className={`flex-1 min-w-0 ${expanded ? 'whitespace-pre-wrap break-all' : 'truncate'}`}>
              <span className="text-[hsl(var(--code-prompt))] select-none">$ </span>
              <HighlightedCode language="bash" code={command} className="text-xs" />
            </div>
            {/* Expand hint — shows line count and chevron when collapsed with output */}
            {hasOutput && !expanded && (
              <span className="flex items-center gap-1 shrink-0 text-[hsl(var(--code-fg-subtle))]">
                <span className="font-sans text-2xs">{outputLines} line{outputLines !== 1 ? 's' : ''}</span>
                <ChevronRight className="w-3 h-3" />
              </span>
            )}
            {hasOutput && expanded && (
              <ChevronRight className="w-3 h-3 shrink-0 text-[hsl(var(--code-fg-subtle))] rotate-90 transition-transform" />
            )}
          </div>
          <CopyButton text={command} className="absolute right-2 top-1.5 p-1 rounded text-[hsl(var(--code-fg-subtle))] hover:text-[hsl(var(--code-fg))] transition-all opacity-50 group-hover/copy:opacity-100 focus:opacity-100" />
        </div>

        {/* Output — expanded only */}
        {expanded && hasOutput && (
          <div className="group/copy relative border-t border-[hsl(var(--code-border))]">
            <div className="px-3 py-2 max-h-[200px] sm:max-h-[300px] overflow-y-auto">
              {stdout && (
                <pre className="text-xs text-[hsl(var(--code-fg))] whitespace-pre-wrap break-all leading-relaxed">
                  {stdout}
                </pre>
              )}
              {stderr && (
                <pre className="text-xs text-[hsl(var(--code-stderr))] whitespace-pre-wrap break-all leading-relaxed mt-1">
                  {stderr}
                </pre>
              )}
            </div>
            <CopyButton
              text={[stdout, stderr].filter(Boolean).join('\n')}
              className="absolute right-2 top-1.5 p-1 rounded text-[hsl(var(--code-fg-subtle))] hover:text-[hsl(var(--code-fg))] transition-all opacity-50 group-hover/copy:opacity-100 focus:opacity-100"
            />
          </div>
        )}
      </div>
    </div>
  );
}
