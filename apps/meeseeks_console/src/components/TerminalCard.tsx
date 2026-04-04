import { useState } from 'react';
import { CheckCircle2, XCircle, ChevronRight } from 'lucide-react';
import { CopyButton } from './CopyButton';

interface TerminalCardProps {
  command: string;
  cwd?: string;
  exitCode?: number;
  stdout?: string;
  stderr?: string;
  durationMs?: number;
  defaultExpanded?: boolean;
}

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const m = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return `${m}m ${s}s`;
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

// Fixed dark backgrounds so the terminal stays dark in both light and dark themes.
// --bash-bg and --surface-deep become white/cream in light mode, which breaks
// the "always looks like a terminal" design intent.
const TITLE_BG = 'bg-[hsl(220_5%_12%)]';   // ~#1c1d1e — dark chrome bar
const BODY_BG = 'bg-[hsl(220_5%_7%)]';      // ~#101112 — deep terminal body

export function TerminalCard({
  command,
  cwd,
  exitCode,
  stdout,
  stderr,
  durationMs,
  defaultExpanded = false,
}: TerminalCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const isError = exitCode !== undefined && exitCode !== 0;
  const hasOutput = !!(stdout || stderr);
  const outputLines = countLines(stdout || '') + countLines(stderr || '');

  return (
    <div
      className={`rounded-lg overflow-hidden font-mono border-l-[3px] transition-colors ${
        hasOutput ? 'cursor-pointer' : ''
      } ${
        isError ? 'border-l-red-500' : 'border-l-emerald-500/60'
      }`}
      onClick={() => hasOutput && setExpanded((p) => !p)}
    >
      {/* Title bar — terminal window chrome */}
      <div className={`flex items-center gap-2 px-3 py-1.5 ${TITLE_BG}`}>
        {/* Traffic-light dots */}
        <div className="flex items-center gap-1.5 shrink-0">
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-red-500' : 'bg-emerald-500'}`} />
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-red-500/30' : 'bg-white/15'}`} />
          <span className={`w-2.5 h-2.5 rounded-full ${isError ? 'bg-red-500/30' : 'bg-white/15'}`} />
        </div>

        {/* CWD tab title */}
        {cwd && (
          <span
            className="text-[11px] text-white/50 truncate flex-1 min-w-0"
            title={cwd}
          >
            {shortenCwd(cwd)}
          </span>
        )}
        {!cwd && <span className="flex-1" />}

        {/* Duration */}
        {durationMs !== undefined && (
          <span className="text-[10px] text-white/40 shrink-0 hidden sm:inline">
            {formatDuration(durationMs)}
          </span>
        )}

        {/* Status indicator — only shown when exit code is known */}
        {exitCode !== undefined && (
          <span className="shrink-0">
            {isError ? (
              <span className="flex items-center gap-1">
                <XCircle className="w-3.5 h-3.5 text-red-400" />
                <span className="text-[10px] text-red-400">{exitCode}</span>
              </span>
            ) : (
              <CheckCircle2 className="w-3.5 h-3.5 text-emerald-500/70" />
            )}
          </span>
        )}
      </div>

      {/* Terminal body */}
      <div className={BODY_BG}>
        {/* Command line — always visible */}
        <div className="group/copy relative px-3 py-2">
          <div className="flex items-center gap-2">
            <div className={`flex-1 min-w-0 ${expanded ? 'whitespace-pre-wrap break-all' : 'truncate'}`}>
              <span className="text-emerald-400/70 select-none">$ </span>
              <span className="text-white/90 text-xs">{command}</span>
            </div>
            {/* Expand hint — shows line count and chevron when collapsed with output */}
            {hasOutput && !expanded && (
              <span className="flex items-center gap-1 shrink-0 text-white/25">
                <span className="text-[10px]">{outputLines} line{outputLines !== 1 ? 's' : ''}</span>
                <ChevronRight className="w-3 h-3" />
              </span>
            )}
            {hasOutput && expanded && (
              <ChevronRight className="w-3 h-3 shrink-0 text-white/25 rotate-90 transition-transform" />
            )}
          </div>
          <CopyButton text={command} className="absolute right-2 top-1.5 p-1 rounded text-white/40 hover:text-white/80 transition-all opacity-0 group-hover/copy:opacity-100 focus:opacity-100" />
        </div>

        {/* Output — expanded only */}
        {expanded && hasOutput && (
          <div className="group/copy relative border-t border-white/5">
            <div className="px-3 py-2 max-h-[200px] sm:max-h-[300px] overflow-y-auto">
              {stdout && (
                <pre className="text-xs text-white/75 whitespace-pre-wrap break-all leading-relaxed">
                  {stdout}
                </pre>
              )}
              {stderr && (
                <pre className="text-xs text-red-400/80 whitespace-pre-wrap break-all leading-relaxed mt-1">
                  {stderr}
                </pre>
              )}
            </div>
            <CopyButton
              text={[stdout, stderr].filter(Boolean).join('\n')}
              className="absolute right-2 top-1.5 p-1 rounded text-white/40 hover:text-white/80 transition-all opacity-0 group-hover/copy:opacity-100 focus:opacity-100"
            />
          </div>
        )}
      </div>
    </div>
  );
}
