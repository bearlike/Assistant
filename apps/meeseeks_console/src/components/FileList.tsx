import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { DiffStats } from './DiffStats';
import { DiffFile } from '../types';
interface FileListProps {
  files: DiffFile[];
  onFileClick?: (file: DiffFile) => void;
}
export function FileList({ files, onFileClick }: FileListProps) {
  const [isOpen, setIsOpen] = useState(true);
  return (
    <div className="mt-4 border border-[hsl(var(--border))] rounded-lg bg-[hsl(var(--card))] overflow-hidden">
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="w-full flex items-center justify-between px-3 py-2 bg-[hsl(var(--muted))]/50 hover:bg-[hsl(var(--muted))] transition-colors">

        <div className="flex items-center gap-2 text-xs font-medium text-[hsl(var(--foreground))]">
          <span>Files ({files.length})</span>
        </div>
        {isOpen ?
        <ChevronDown className="w-3 h-3 text-[hsl(var(--muted-foreground))]" /> :

        <ChevronRight className="w-3 h-3 text-[hsl(var(--muted-foreground))]" />
        }
      </button>

      {isOpen &&
      <div className="divide-y divide-[hsl(var(--border))]/50">
          {files.map((file, i) =>
        <div
          key={`${file.path}-${i}`}
          onClick={() => onFileClick?.(file)}
          className="flex items-center justify-between px-3 py-2 hover:bg-[hsl(var(--accent))] transition-colors group cursor-pointer">

              <div className="flex items-center gap-2 text-xs">
                <span className="text-[hsl(var(--foreground))] font-medium">
                  {file.name}
                </span>
                <span className="text-[hsl(var(--muted-foreground))]">
                  {file.path}
                </span>
              </div>
              <DiffStats
            additions={file.additions}
            deletions={file.deletions} />

            </div>
        )}
        </div>
      }
    </div>);

}