/**
 * Tests for WikiBadge — the atomic class behind the README "Copy badge"
 * affordance. Pins the markdown shape, the absolute-URL composition (incl.
 * the platform param), and the null-gating contract callers rely on.
 */
import { describe, expect, it } from "vitest";

import { WikiBadge } from "@/components/wiki/badge";
import type { PlatformId } from "@/components/wiki/router";

const ORIGIN = "https://mewbo.example.com";

/** Build a badge, asserting (and narrowing away) the null gate. */
function build(args: {
  slug?: string;
  pageId?: string;
  platform?: PlatformId;
  origin?: string;
}): WikiBadge {
  const badge = WikiBadge.forPage({ origin: ORIGIN, ...args });
  if (!badge) throw new Error(`expected a badge for ${JSON.stringify(args)}`);
  return badge;
}

describe("WikiBadge.forPage", () => {
  it("composes an absolute link to the wiki page from an explicit origin", () => {
    const badge = build({
      slug: "git.example.com/acme/widgets",
      pageId: "overview",
      platform: "gitea",
    });
    expect(badge.linkUrl).toBe(
      `${ORIGIN}/wiki/p/overview?slug=${encodeURIComponent(
        "git.example.com/acme/widgets",
      )}&platform=gitea`,
    );
  });

  it("carries the static artwork + alt into GFM markdown", () => {
    const badge = build({ slug: "acme/widgets", pageId: "home" });
    expect(badge.markdown).toBe(
      `[![${WikiBadge.ALT}](${WikiBadge.IMAGE_URL})](${badge.linkUrl})`,
    );
    // Artwork is the static CDN SVG regardless of repo.
    expect(badge.markdown).toContain(WikiBadge.IMAGE_URL);
  });

  it("omits the platform param when none is given", () => {
    const badge = build({ slug: "acme/widgets", pageId: "home" });
    expect(badge.linkUrl).not.toContain("platform=");
  });

  it("returns null when slug or pageId is missing (gates the affordance)", () => {
    expect(WikiBadge.forPage({ pageId: "home", origin: ORIGIN })).toBeNull();
    expect(WikiBadge.forPage({ slug: "acme/widgets", origin: ORIGIN })).toBeNull();
    expect(WikiBadge.forPage({ origin: ORIGIN })).toBeNull();
  });
});
