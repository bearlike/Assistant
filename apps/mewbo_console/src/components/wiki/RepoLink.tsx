/**
 * Anchor that takes the user out to the source repository on its native
 * host. Slug is the canonical identity (``host/owner/repo``); the host
 * segment is where we link to — no platform-name guessing, no hard-coded
 * defaults. Renders the visible label with a dotted underline so it's
 * clearly clickable without competing with conventional text links.
 *
 * Resolution order for the destination URL:
 *   1. Caller-supplied ``repoUrl`` (most accurate — preserves protocol
 *      and any non-standard path the user actually typed).
 *   2. ``https://{host}/{owner}/{repo}`` built from the slug.
 *   3. None — render as plain text. We never fabricate a link from the
 *      platform name when the host is missing (legacy slugs only).
 *
 * Click stops propagation so an enclosing tile's onClick doesn't fire
 * and double-navigate.
 */

import { canonicalRepoUrl, parseSlug, shortSlug } from "./slug";

interface RepoLinkProps {
  slug: string;
  /** Persisted repo URL — preferred over deriving from slug. */
  repoUrl?: string;
  /** Display either the short ``owner/repo`` form or the full slug. */
  display?: "short" | "full";
  className?: string;
}

export function RepoLink({
  slug,
  repoUrl,
  display = "full",
  className = "",
}: RepoLinkProps) {
  const href = canonicalRepoUrl(slug, repoUrl);
  const label = display === "short" ? shortSlug(slug) : slug;
  const dotted =
    "underline decoration-dotted underline-offset-[3px] decoration-[hsl(var(--muted-foreground))]/60 hover:decoration-[hsl(var(--foreground))]";
  if (!href) {
    return <span className={className}>{label}</span>;
  }
  const host = parseSlug(slug)?.host;
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      title={host ? `Open on ${host}` : `Open ${label}`}
      className={`${dotted} ${className}`}
    >
      {label}
    </a>
  );
}
