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

export function lineStyles(type: ParsedLine['type']): { bg: string; text: string; prefix: string } {
  switch (type) {
    case 'insert':
      return { bg: 'bg-emerald-500/[0.12]', text: 'text-emerald-300', prefix: '+' };
    case 'delete':
      return { bg: 'bg-red-500/[0.12]', text: 'text-red-300', prefix: '-' };
    default:
      return { bg: '', text: 'text-white/60', prefix: ' ' };
  }
}
