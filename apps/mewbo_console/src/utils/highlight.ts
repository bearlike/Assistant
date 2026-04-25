// Thin wrapper around highlight.js core so we ship only the languages we use.
// hljs is already in our bundle via rehype-highlight → lowlight, so this adds
// no new runtime cost — it just exposes a direct programmatic API for places
// that aren't going through markdown (e.g. TerminalCard's bash command,
// FileReadCard's file viewer).
//
// Output is plain HTML (`<span class="hljs-…">`). Render via
// dangerouslySetInnerHTML — the markup surface is well-known and safe.

import hljs from 'highlight.js/lib/core';
import bash from 'highlight.js/lib/languages/bash';
import shell from 'highlight.js/lib/languages/shell';
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import json from 'highlight.js/lib/languages/json';
import yaml from 'highlight.js/lib/languages/yaml';
import css from 'highlight.js/lib/languages/css';
import xml from 'highlight.js/lib/languages/xml'; // covers html/xml/svg
import markdown from 'highlight.js/lib/languages/markdown';
import go from 'highlight.js/lib/languages/go';
import rust from 'highlight.js/lib/languages/rust';
import java from 'highlight.js/lib/languages/java';
import sql from 'highlight.js/lib/languages/sql';
import dockerfile from 'highlight.js/lib/languages/dockerfile';

hljs.registerLanguage('bash', bash);
hljs.registerLanguage('sh', bash);
hljs.registerLanguage('shell', shell);
hljs.registerLanguage('zsh', bash);
hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('json', json);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('css', css);
hljs.registerLanguage('xml', xml);
hljs.registerLanguage('markdown', markdown);
hljs.registerLanguage('go', go);
hljs.registerLanguage('rust', rust);
hljs.registerLanguage('java', java);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('dockerfile', dockerfile);

// Extension → hljs language id. Unknown extensions fall through to plain
// rendering (no highlighting). Keep this map small and obvious — not every
// niche language needs an entry; hljs falls back gracefully.
const EXT_TO_LANG: Record<string, string> = {
  js: 'javascript',
  mjs: 'javascript',
  cjs: 'javascript',
  jsx: 'javascript',
  ts: 'typescript',
  tsx: 'typescript',
  py: 'python',
  pyi: 'python',
  json: 'json',
  jsonc: 'json',
  yaml: 'yaml',
  yml: 'yaml',
  css: 'css',
  scss: 'css',
  html: 'xml',
  htm: 'xml',
  xml: 'xml',
  svg: 'xml',
  vue: 'xml',
  md: 'markdown',
  mdx: 'markdown',
  markdown: 'markdown',
  go: 'go',
  rs: 'rust',
  java: 'java',
  kt: 'java', // kotlin highlights reasonably well as java
  sql: 'sql',
  dockerfile: 'dockerfile',
  sh: 'bash',
  bash: 'bash',
  zsh: 'bash',
  env: 'bash',
};

export function languageForExtension(filename: string): string | undefined {
  const base = filename.split('/').pop()?.toLowerCase() ?? '';
  if (base === 'dockerfile') return 'dockerfile';
  const ext = base.includes('.') ? base.split('.').pop() ?? '' : '';
  return EXT_TO_LANG[ext];
}

export function highlight(code: string, language: string): { __html: string } {
  if (!hljs.getLanguage(language)) {
    return { __html: escapeHtml(code) };
  }
  try {
    return { __html: hljs.highlight(code, { language, ignoreIllegals: true }).value };
  } catch {
    return { __html: escapeHtml(code) };
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}
