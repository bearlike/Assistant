/**
 * `@mention` input parser for the chat composer.
 *
 * Mirrors `lib/commands.ts` in spirit: a tiny pure helper the composer calls on
 * every keystroke to decide whether to open the file-mention picker, and a
 * splice helper to insert the chosen path. The backend expands `@<ref>` at
 * submit time — this only helps the user *compose* the token.
 */

export interface ParsedMention {
  /** The text typed after the active `@` (may be empty right after `@`). */
  query: string;
  /** Index of the `@` in the source text (where the splice begins). */
  start: number;
}

/**
 * Find the active `@token` under the caret, or null when there isn't one.
 *
 * A token is active when, scanning left from the caret:
 *   - we hit an `@` with no intervening whitespace, AND
 *   - that `@` is at the start of the text or follows whitespace
 *     (so `email@host` — `@` after a word char — is ignored).
 *
 * The query is everything between the `@` and the caret; it must itself be
 * whitespace-free (typing a space ends the mention). Paths can contain `/`,
 * `.`, `-`, `_` etc., so only whitespace terminates the token.
 */
export function parseMentionInput(
  text: string,
  caret: number,
): ParsedMention | null {
  // Clamp the caret into range so callers can pass selectionStart verbatim.
  const pos = Math.max(0, Math.min(caret, text.length));
  // Walk left from the caret to the nearest `@` or whitespace.
  let i = pos - 1;
  while (i >= 0) {
    const ch = text[i];
    if (ch === "@") break;
    if (/\s/.test(ch)) return null; // whitespace before an `@` ⇒ no active token
    i -= 1;
  }
  if (i < 0 || text[i] !== "@") return null;
  // The `@` must start the text or follow whitespace — not be part of a word
  // (rejects `email@host`).
  const prev = i > 0 ? text[i - 1] : "";
  if (prev !== "" && !/\s/.test(prev)) return null;
  return { query: text.slice(i + 1, pos), start: i };
}

export interface SplicedMention {
  /** The new textarea value with `@query` replaced by `@path `. */
  value: string;
  /** Where to place the caret afterwards (just past the inserted trailing space). */
  caret: number;
}

/**
 * Replace the active `@query` (located at `mention.start`, ending at `caret`)
 * with `@<path> ` and return the new value plus the caret position to restore.
 * A trailing space terminates the token and readies the next word.
 */
export function spliceMention(
  text: string,
  caret: number,
  mention: ParsedMention,
  path: string,
): SplicedMention {
  const pos = Math.max(0, Math.min(caret, text.length));
  const before = text.slice(0, mention.start);
  const after = text.slice(pos);
  const token = `@${path} `;
  // Avoid doubling a space the user already typed right after the caret.
  const joinedAfter = after.startsWith(" ") ? after.slice(1) : after;
  return {
    value: `${before}${token}${joinedAfter}`,
    caret: before.length + token.length,
  };
}
