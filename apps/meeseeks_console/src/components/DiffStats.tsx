import React from 'react';
interface DiffStatsProps {
  additions?: number;
  deletions?: number;
  className?: string;
}
export function DiffStats({
  additions = 0,
  deletions = 0,
  className = ''
}: DiffStatsProps) {
  return <div className={`flex items-center gap-2 text-xs font-mono ${className}`}>
      {additions > 0 && <span className="text-green-500">+{additions}</span>}
      {deletions > 0 && <span className="text-red-500">-{deletions}</span>}
    </div>;
}