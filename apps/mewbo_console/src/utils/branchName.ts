/**
 * Client-side default-branch-name generation for a new worktree — shared by
 * the composer's `ConfigMenu` and the Settings `WorktreesPanel`, which both
 * offer the same "create from base" flow and previously carried byte-identical
 * copies of this trio.
 *
 * Mirrors the Mewbo branch convention used by the API. Keeping this literal
 * here (rather than fetching it) avoids a round-trip just for a 6-char
 * prefix; the only place it's authoritative is the Python worktree module.
 */
export const MEWBO_BRANCH_PREFIX = 'mewbo/';

/** Slugify a branch name into a directory-safe token. Mirrors the Python
 * ``slugify_branch`` helper closely enough for default-name generation. */
export function slugifyBranchClient(branch: string): string {
  return branch
    .trim()
    .replace(/[^A-Za-z0-9._-]+/g, '-')
    .replace(/^[-._]+|[-._]+$/g, '')
    || 'branch';
}

/** Browser-compatible 6-hex token. ``crypto.randomUUID`` is widely
 * available; we slice it for compactness. Falls back to ``Math.random`` so
 * tests and ancient environments don't crash. */
export function shortId(): string {
  const c = (globalThis as unknown as { crypto?: Crypto }).crypto;
  if (c?.getRandomValues) {
    const bytes = new Uint8Array(3);
    c.getRandomValues(bytes);
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  }
  return Math.floor(Math.random() * 0xffffff).toString(16).padStart(6, '0');
}

export function defaultMewboBranchName(base: string): string {
  return `${MEWBO_BRANCH_PREFIX}${slugifyBranchClient(base)}-${shortId()}`;
}
