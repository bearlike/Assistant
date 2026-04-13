// Pretty-print a string if (and only if) it parses as JSON. Otherwise
// return the original. Used to make MCP tool-result blobs readable
// without changing the underlying data.
export function prettyJsonIfValid(text: string | undefined | null): string {
  if (!text || typeof text !== 'string') return text ?? '';
  const t = text.trim();
  // Quick reject: must start with `{`, `[`, or `"` (JSON value).
  if (!t || (t[0] !== '{' && t[0] !== '[' && t[0] !== '"')) return text;
  try {
    return JSON.stringify(JSON.parse(t), null, 2);
  } catch {
    return text;
  }
}
