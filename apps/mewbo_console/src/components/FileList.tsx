import { useState } from 'react';
import { ChevronDown } from 'lucide-react';
import { DiffFile } from '../types';

interface FileListProps {
  files: DiffFile[];
  onFileClick?: (file: DiffFile) => void;
}

function truncMid(s: string, head = 22, tail = 24): string {
  if (!s || s.length <= head + tail + 1) return s;
  return s.slice(0, head) + '…' + s.slice(-tail);
}

export function FileList({ files, onFileClick }: FileListProps) {
  const [open, setOpen] = useState(true);
  if (!files || files.length === 0) return null;

  return (
    <section
      className="mt-4 pt-3.5 border-t border-[hsl(var(--border))]"
      aria-label={`Files written (${files.length})`}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="group/cap inline-flex items-center gap-2 h-6 -ml-1 px-1.5 rounded-md text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/45 transition-colors"
      >
        <ChevronDown
          className={`w-3 h-3 transition-transform duration-200 ${open ? '' : '-rotate-90'}`}
          aria-hidden
        />
        <span className="inline-flex items-center gap-2 font-mono text-[11px] font-medium uppercase tracking-[0.04em]">
          Files written
          <span className="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-[4px] bg-[hsl(var(--muted))] text-[10px] text-[hsl(var(--muted-foreground))] normal-case tracking-normal">
            {files.length}
          </span>
        </span>
      </button>

      {open && (
        <ul role="list" className="mt-1 flex flex-col">
          {files.map((file, i) => (
            <li key={`${file.path}-${i}`} className="m-0">
              <button
                type="button"
                onClick={() => onFileClick?.(file)}
                title={file.path}
                className="grid grid-cols-[auto_1fr_auto] items-center gap-3 w-full h-9 pl-2 pr-2.5 rounded-md text-left hover:bg-[hsl(var(--accent))]/55 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/45 transition-colors"
              >
                <span className="font-mono text-[12.5px] font-medium text-[hsl(var(--foreground))]">
                  {file.name}
                </span>
                <span
                  className="font-mono text-[11px] text-[hsl(var(--muted-foreground))] overflow-hidden text-ellipsis whitespace-nowrap min-w-0"
                  aria-hidden
                >
                  {truncMid(file.path)}
                </span>
                {typeof file.additions === 'number' && file.additions > 0 && (
                  <span
                    className="font-mono text-[11px] font-medium text-[hsl(var(--diff-add-text))]"
                    aria-label={`${file.additions} additions`}
                  >
                    <span className="opacity-85 mr-px">+</span>
                    {file.additions}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
