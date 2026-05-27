/**
 * Client-side validation for plugin marketplace repository entries.
 *
 * MIRRORS the backend resolver
 * `packages/mewbo_core/src/mewbo_core/plugins.py::_resolve_git_url`
 * (cross-ref Gitea #9 / #14 — the host-agnostic marketplace backend) so the
 * console accepts exactly the forms the backend can clone:
 *
 *   - a full git URL (https / http / ssh / git scheme), used verbatim;
 *   - an scp-style SSH ref `git@host:owner/repo`, used verbatim;
 *   - `host/owner/repo` where the first segment looks like a host
 *     (contains a `.` or a `:port`) → `https://host/owner/repo.git`;
 *   - a bare `owner/repo` (optional trailing `.git`) → default host.
 *
 * It is INTENTIONALLY slightly stricter than the backend: `_resolve_git_url`
 * would happily turn a single token like `foo` into
 * `https://github.com/foo.git` (a bad clone URL that fails at clone time),
 * whereas this requires at least `owner/repo`. Catching that typo here gives
 * the user an inline error instead of a silent clone failure later.
 */

/** Full URL scheme: https/http/ssh/git — case-insensitive (mirrors `_URL_SCHEME_RE`). */
const URL_SCHEME_RE = /^(?:https?|ssh|git):\/\//i;

/** scp-style SSH ref, e.g. `git@github.com:owner/repo` (mirrors `_SCP_LIKE_RE`). */
const SCP_LIKE_RE = /^[A-Za-z0-9._+-]+@[A-Za-z0-9._-]+:/;

/** A path segment looks like a git host when it has a dot or an explicit port. */
function looksLikeHost(segment: string): boolean {
  return segment.includes(".") || segment.includes(":");
}

/**
 * Validate a single marketplace entry. Returns an error message string when
 * the entry is not a form the backend can resolve, or `null` when it is valid.
 */
export function validateMarketplaceEntry(entry: string): string | null {
  const trimmed = entry.trim();
  if (!trimmed) {
    return "Repository cannot be empty";
  }

  // Full git URL or scp-style SSH ref → valid (used verbatim by the backend).
  if (URL_SCHEME_RE.test(trimmed) || SCP_LIKE_RE.test(trimmed)) {
    return null;
  }

  const parts = trimmed.split("/");

  // `host/owner/repo` — first segment looks like a host (dot or :port).
  if (parts.length >= 3 && looksLikeHost(parts[0])) {
    return null;
  }

  // Bare `owner/repo` (optional trailing `.git`) — exactly two non-empty
  // segments. Stricter than the backend, which would accept a single token.
  if (parts.length === 2 && parts[0] !== "" && parts[1] !== "") {
    return null;
  }

  return "Use a git URL, host/owner/repo, or owner/repo";
}
