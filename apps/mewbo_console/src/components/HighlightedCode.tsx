import { highlight } from '../utils/highlight';

interface HighlightedCodeProps {
  code: string;
  language: string;
  className?: string;
}

// Inline syntax-highlighted code. Style the surrounding container at the
// call site (font, size, wrapping). The `.hljs-*` classes pick up theme
// colors from the CSS variables in `src/index.css`.
export function HighlightedCode({ code, language, className }: HighlightedCodeProps) {
  return (
    <code
      className={`hljs ${className ?? ''}`}
      dangerouslySetInnerHTML={highlight(code, language)}
    />
  );
}
