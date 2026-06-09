/**
 * "Ask Mewbo Wiki" repository badge — the snippet a maintainer drops into
 * their README so visitors can browse (and ask questions about) the code.
 *
 * The artwork is a single static CDN SVG shared by every repo; only the
 * link target is per-repo. This atomic class is the one home for the badge
 * constants, the absolute-URL composition, and the markdown so no caller
 * hand-builds the snippet inline (folder convention — cf. `IndexingProgress`,
 * `RelativeTime`).
 */

import { buildHref, type PlatformId } from "./router";

export class WikiBadge {
  /** Static badge artwork — identical for every repository. */
  static readonly IMAGE_URL =
    "https://cdn.thekrishna.in/img/Badge-Ask-Mewbo-Wiki.svg";
  /** Image alt text / markdown label. */
  static readonly ALT = "Ask Mewbo Wiki";

  private constructor(
    /** Absolute URL the badge links to (this deployment's wiki page). */
    readonly linkUrl: string,
  ) {}

  /**
   * Compose a badge for a repo's wiki page. ``origin`` defaults to the live
   * deployment origin so a pasted badge points back here; pass it explicitly
   * in tests / SSR. Returns ``null`` when there's no resolvable page to land
   * on — callers use that to gate the affordance.
   */
  static forPage(args: {
    slug?: string;
    pageId?: string;
    platform?: PlatformId;
    origin?: string;
  }): WikiBadge | null {
    if (!args.slug || !args.pageId) return null;
    const origin =
      args.origin ??
      (typeof window !== "undefined" ? window.location.origin : "");
    const href = buildHref({
      kind: "page",
      pageId: args.pageId,
      slug: args.slug,
      platform: args.platform,
    });
    return new WikiBadge(`${origin}${href}`);
  }

  /** GitHub-flavoured markdown — the README snippet (`[![alt](img)](link)`). */
  get markdown(): string {
    return `[![${WikiBadge.ALT}](${WikiBadge.IMAGE_URL})](${this.linkUrl})`;
  }
}
