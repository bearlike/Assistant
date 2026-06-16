/**
 * Per-source brand identity for result cards + trace lanes.
 *
 * The search catalog is live (configured MCP servers), so source ids are not a
 * closed set — but the well-known servers (`github`, `gitea`,
 * `internet-search`/searxng, `docs-deepwiki`, `docs-context7`, `huggingface`,
 * `gitmcp`) have official CC0 brand marks via the `simple-icons` package we
 * already ship (see wiki `PlatformIcon`). This module resolves an id to its
 * brand glyph + brand-ish color; an unknown id falls back to the catalog's own
 * letter glyph (the `SrcAvatar` default), so nothing ever renders blank.
 *
 * One curated map, no per-source JSX — `SrcAvatar` renders the SVG path with
 * `currentColor` (the tile sets the color). Matching is by NORMALIZED id
 * substring so `mcp_github`, `github`, `github-pr` all resolve to GitHub.
 */
import {
  siGit,
  siGitea,
  siGithub,
  siHuggingface,
  siReadthedocs,
  siSearxng,
  type SimpleIcon,
} from "simple-icons"

export interface SourceBrand {
  /** simple-icons path data (`viewBox="0 0 24 24"`). */
  path: string
  /** Brand hex (`#RRGGBB`) for the tile background tint + glyph. */
  hex: string
  /** Accessible brand name. */
  title: string
}

// DeepWiki / Context7 have no official simple-icon; reuse a neutral docs mark
// (Read the Docs) so they still read as a documentation source rather than a
// bare letter. Searxng covers the `internet-search` server id too.
function brand(icon: SimpleIcon): SourceBrand {
  return { path: icon.path, hex: `#${icon.hex}`, title: icon.title }
}

// Ordered longest-key-first so a more specific id wins (e.g. `docs-deepwiki`
// before a hypothetical `docs`). Keys are matched as substrings of the
// normalized (lowercased) source id.
const BRAND_BY_KEY: ReadonlyArray<readonly [string, SourceBrand]> = [
  ["huggingface", brand(siHuggingface)],
  ["docs-deepwiki", brand(siReadthedocs)],
  ["docs-context7", brand(siReadthedocs)],
  ["deepwiki", brand(siReadthedocs)],
  ["context7", brand(siReadthedocs)],
  ["internet-search", brand(siSearxng)],
  ["searxng", brand(siSearxng)],
  ["gitmcp", brand(siGit)],
  ["github", brand(siGithub)],
  ["gitea", brand(siGitea)],
]

/**
 * Resolve a source id to its known brand mark, or `null` when the id isn't a
 * recognized server (the caller falls back to the catalog letter glyph).
 */
export function sourceBrand(sourceId: string | undefined): SourceBrand | null {
  if (!sourceId) return null
  const norm = sourceId.toLowerCase()
  for (const [key, value] of BRAND_BY_KEY) {
    if (norm.includes(key)) return value
  }
  return null
}
