/**
 * Slash-command input parser.
 *
 * Returns the lower-cased command name and any whitespace-separated arguments
 * if the input starts with ``/``. The returned name may be a partial command
 * (the palette uses it for prefix-matching while the user is still typing).
 */
export interface ParsedCommand {
  name: string;
  args: string[];
}

export function parseCommandInput(input: string): ParsedCommand | null {
  const trimmed = input.trim();
  if (!trimmed.startsWith("/")) return null;
  const parts = trimmed.slice(1).split(/\s+/);
  const [head, ...rest] = parts;
  if (!head) return null;
  return { name: head.toLowerCase(), args: rest };
}
