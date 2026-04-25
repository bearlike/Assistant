import { ParsedLine } from "../types";

/** Allowed file extensions for file-icon-vectors vivid CDN icons. */
const KNOWN_EXTENSIONS = new Set([
  'js','ts','jsx','tsx','py','rb','go','rs','java','kt','swift',
  'c','cpp','h','cs','php','html','css','scss','json','yaml','yml',
  'toml','xml','sql','sh','bash','zsh','md','txt','env','dockerfile',
  'vue','svelte',
]);

export function fileIconClass(filename: string): string {
  const base = (filename.split('/').pop() || filename).toLowerCase();
  if (base === 'dockerfile') return 'fiv-viv fiv-icon-dockerfile';
  const ext = base.includes('.') ? base.split('.').pop() || '' : '';
  return `fiv-viv fiv-icon-${KNOWN_EXTENSIONS.has(ext) ? ext : 'blank'}`;
}

export function basename(path: string): string {
  return path.split('/').pop() || path;
}

// Theme-aware via --diff-add-* / --diff-del-* / --code-fg-muted (see src/index.css).
export function lineStyles(type: ParsedLine['type']): { bg: string; text: string; prefix: string } {
  switch (type) {
    case 'insert':
      return { bg: 'bg-[hsl(var(--diff-add-bg))]', text: 'text-[hsl(var(--diff-add-text))]', prefix: '+' };
    case 'delete':
      return { bg: 'bg-[hsl(var(--diff-del-bg))]', text: 'text-[hsl(var(--diff-del-text))]', prefix: '-' };
    default:
      return { bg: '', text: 'text-[hsl(var(--code-fg-muted))]', prefix: ' ' };
  }
}
