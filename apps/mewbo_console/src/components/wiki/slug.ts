/**
 * Canonical wiki identity = ``host/owner/repo`` (or, for legacy records,
 * ``owner/repo`` with no host). All helpers here parse and compose around
 * that single shape so the rest of the app never resorts to platform-name
 * fallbacks or hard-coded host tables.
 */

export interface ParsedSlug {
  /** DNS host (``github.com``, ``git.hurricane.home``) — absent on legacy
   *  two-segment slugs from before the canonical refactor. */
  host?: string;
  owner: string;
  repo: string;
}

/**
 * Parse a slug into its components.
 *
 * - ``host/owner/repo`` → fully qualified (3+ segments; intermediate
 *   segments are folded into the host, supporting paths like
 *   ``gitlab.example.io/group/subgroup/repo`` if ever needed by joining
 *   everything before the last two segments back together for ``host``).
 * - ``owner/repo`` → legacy; ``host`` is ``undefined``.
 *
 * Returns ``null`` when the input doesn't have at least owner + repo.
 */
export function parseSlug(slug: string): ParsedSlug | null {
  const parts = slug.split("/").filter(Boolean);
  if (parts.length < 2) return null;
  const repo = parts[parts.length - 1].replace(/\.git$/, "");
  const owner = parts[parts.length - 2];
  if (parts.length === 2) return { owner, repo };
  return {
    host: parts.slice(0, -2).join("/"),
    owner,
    repo,
  };
}

/**
 * Build the canonical fully-qualified slug from a repo URL. We use the
 * URL's host + path (never a guess from the platform name).
 *
 * - Input: ``https://git.hurricane.home/bearlike/Grove``
 * - Output: ``git.hurricane.home/bearlike/Grove``
 *
 * Returns ``null`` when the URL doesn't carry both an owner and repo.
 */
export function slugFromRepoUrl(url: string): string | null {
  try {
    const u = new URL(url.trim());
    const parts = u.pathname.replace(/^\/+|\/+$/g, "").split("/").filter(Boolean);
    if (parts.length < 2) return null;
    const owner = parts[0];
    const repo = parts[1].replace(/\.git$/, "");
    return `${u.hostname}/${owner}/${repo}`;
  } catch {
    return null;
  }
}

/**
 * Compose the canonical external repo URL from slug or persisted repoUrl.
 *
 * Prefers the persisted ``repoUrl`` (handles non-default protocols,
 * trailing paths, etc.) and only falls back to ``https://{host}/{owner}/{repo}``
 * when needed. Returns ``undefined`` for legacy slugs without a host —
 * we never fabricate a github.com link.
 */
export function canonicalRepoUrl(
  slug: string,
  repoUrl?: string,
): string | undefined {
  if (repoUrl) return repoUrl;
  const parsed = parseSlug(slug);
  if (!parsed || !parsed.host) return undefined;
  return `https://${parsed.host}/${parsed.owner}/${parsed.repo}`;
}

/**
 * Short display label — what users typically expect to see when scanning
 * a list of repos. ``owner/repo`` (two segments), with host shown
 * separately as a subtitle so the eye doesn't have to parse the URL.
 */
export function shortSlug(slug: string): string {
  const parsed = parseSlug(slug);
  if (!parsed) return slug;
  return `${parsed.owner}/${parsed.repo}`;
}
