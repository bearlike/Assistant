import { useMemo, useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { DiffFile, TurnMeta } from '../types';
import { parseDiffHunks } from '../utils/diff';
import { useGitDiff } from '../hooks/useGitDiff';
import { DiffStats } from './DiffStats';
import { FileDiffSection } from './DiffCard';
import { fileIconClass, basename } from '../utils/diffCard';

// Theme-aware code surfaces — see --code-chrome / --code-body in src/index.css.
const BODY_BG = 'bg-[hsl(var(--code-body))]';
const TITLE_BG = 'bg-[hsl(var(--code-chrome))]';

type ReviewScope = 'last_turn' | 'uncommitted' | 'branch';

interface ReviewPaneProps {
  sessionId: string;
  selectedTurn: TurnMeta | null;
  sessionFiles: DiffFile[];
}

interface FileEntryProps {
  file: DiffFile;
  expanded: boolean;
  onToggle: () => void;
}

function FileEntry({ file, expanded, onToggle }: FileEntryProps) {
  const parsedFiles = useMemo(() => parseDiffHunks(file.diff ?? ''), [file.diff]);
  const parsed = parsedFiles[0] ?? null;

  const gutterWidth = useMemo(() => {
    if (!parsed) return 3;
    let max = 0;
    for (const h of parsed.hunks) {
      for (const l of h.lines) {
        if (l.oldNumber && l.oldNumber > max) max = l.oldNumber;
        if (l.newNumber && l.newNumber > max) max = l.newNumber;
      }
    }
    return Math.max(3, String(max).length + 1);
  }, [parsed]);

  return (
    <div className={`border-b border-[hsl(var(--code-border))] last:border-b-0`}>
      {/* Header */}
      <button
        onClick={onToggle}
        className={`w-full flex items-center gap-2 px-3 py-2 ${TITLE_BG} hover:brightness-110 transition-[filter] text-left`}
      >
        <span className={`${fileIconClass(file.path)} shrink-0`} style={{ fontSize: '16px' }} />
        <span className="text-[11px] text-[hsl(var(--code-fg))] truncate flex-1 min-w-0" title={file.path}>
          {basename(file.path)}
        </span>
        <DiffStats additions={file.additions} deletions={file.deletions} className="shrink-0" />
        <ChevronRight
          className={`w-3 h-3 shrink-0 text-[hsl(var(--code-fg-subtle))] transition-transform ${expanded ? 'rotate-90' : ''}`}
        />
      </button>

      {/* Body */}
      {expanded && (
        <div className={`${BODY_BG} max-h-[500px] overflow-y-auto border-t border-[hsl(var(--code-border))]`}>
          {parsed ? (
            <FileDiffSection file={parsed} expanded gutterWidth={gutterWidth} />
          ) : (
            <div className="px-3 py-2 text-xs text-[hsl(var(--code-fg-subtle))] font-mono">No diff data.</div>
          )}
        </div>
      )}
    </div>
  );
}

export function ReviewPane({ sessionId, selectedTurn, sessionFiles }: ReviewPaneProps) {
  const [scope, setScope] = useState<ReviewScope>('last_turn');
  const [expandedFiles, setExpandedFiles] = useState<Set<string>>(new Set());

  const gitScope = scope === 'last_turn' ? 'uncommitted' : (scope as 'uncommitted' | 'branch');
  const gitDiff = useGitDiff(sessionId, gitScope, scope !== 'last_turn');

  const files: DiffFile[] = useMemo(() => {
    if (scope === 'last_turn') return selectedTurn?.files ?? [];
    return gitDiff.gitRepo ? gitDiff.files : sessionFiles;
  }, [scope, selectedTurn, gitDiff.gitRepo, gitDiff.files, sessionFiles]);

  const usingFallback = scope !== 'last_turn' && !gitDiff.gitRepo && !gitDiff.loading;

  function handleScopeChange(next: ReviewScope) {
    setScope(next);
    setExpandedFiles(new Set());
  }

  function toggleFile(path: string) {
    setExpandedFiles((prev) => {
      const next = new Set(prev);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
      }
      return next;
    });
  }

  const tabs: { key: ReviewScope; label: string }[] = [
    { key: 'last_turn', label: 'Last turn' },
    { key: 'uncommitted', label: 'Uncommitted' },
    { key: 'branch', label: 'Branch' },
  ];

  return (
    <div className="flex flex-col h-full overflow-hidden">
      {/* Scope selector */}
      <div className="flex items-center gap-6 px-4 h-9 border-b border-[hsl(var(--border-strong))] bg-[hsl(var(--surface))] shrink-0">
        {tabs.map(({ key, label }) => (
          <button
            key={key}
            onClick={() => handleScopeChange(key)}
            className={`h-full text-xs font-medium border-b-2 transition-colors px-1 ${
              scope === key
                ? 'border-[hsl(var(--foreground))] text-[hsl(var(--foreground))]'
                : 'border-transparent text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))]'
            }`}
          >
            {label}
          </button>
        ))}
        {usingFallback && sessionFiles.length > 0 && (
          <span className="ml-auto text-[10px] text-[hsl(var(--muted-foreground))] italic">
            git unavailable — showing session edits
          </span>
        )}
      </div>

      {/* File list */}
      <div className="flex-1 overflow-y-auto">
        {/* Empty state: last_turn with no turn selected */}
        {scope === 'last_turn' && !selectedTurn && (
          <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
            Select a conversation turn to view its file changes.
          </div>
        )}

        {/* Loading */}
        {scope !== 'last_turn' && gitDiff.loading && (
          <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
            Loading…
          </div>
        )}

        {/* No files */}
        {!gitDiff.loading && files.length === 0 && (scope !== 'last_turn' || selectedTurn) && (
          <div className="flex items-center justify-center h-full text-xs text-[hsl(var(--muted-foreground))]">
            {usingFallback && sessionFiles.length === 0
              ? 'No file edits recorded in this session.'
              : 'No changed files.'}
          </div>
        )}

        {/* Accordion */}
        {files.length > 0 && !gitDiff.loading && (
          <div className={`rounded-none font-mono border-0 ${BODY_BG}`}>
            {files.map((file) => (
              <FileEntry
                key={file.path || file.name}
                file={file}
                expanded={expandedFiles.has(file.path || file.name)}
                onToggle={() => toggleFile(file.path || file.name)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
