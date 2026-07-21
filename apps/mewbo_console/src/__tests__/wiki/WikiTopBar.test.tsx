/**
 * Render/interaction coverage for the WikiTopBar "Copy badge" affordance:
 * it is gated on a resolvable badge page, and opening it reveals the live
 * artwork preview + the copyable README markdown.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { WikiTopBar } from "@/components/wiki/WikiTopBar";
import { WikiBadge } from "@/components/wiki/badge";

afterEach(cleanup);

const SLUG = "git.example.com/acme/widgets";

describe("WikiTopBar — Copy badge", () => {
  it("hides the badge affordance when there's no page to land on", () => {
    render(<WikiTopBar repo={SLUG} platform="gitea" />);
    expect(
      screen.queryByRole("button", { name: /copy readme badge/i }),
    ).toBeNull();
  });

  it("reveals the artwork preview + README markdown on open", async () => {
    const user = userEvent.setup();
    render(<WikiTopBar repo={SLUG} platform="gitea" badgePageId="overview" />);

    await user.click(
      screen.getByRole("button", { name: /copy readme badge/i }),
    );

    // Live preview of the static artwork.
    expect(await screen.findByAltText(WikiBadge.ALT)).toHaveAttribute(
      "src",
      WikiBadge.IMAGE_URL,
    );
    // The copyable snippet carries the static image + the per-repo slug.
    const snippet = screen.getByText((t) => t.includes(WikiBadge.IMAGE_URL));
    expect(snippet.textContent).toContain(encodeURIComponent(SLUG));
    expect(snippet.textContent).toContain("/wiki/p/overview");
  });
});

describe("WikiTopBar — responsive chrome", () => {
  it("renders no product wordmark (the nav rail owns product identity)", () => {
    render(<WikiTopBar repo={SLUG} platform="gitea" />);
    expect(screen.queryByText("MewboWiki")).toBeNull();
  });

  // The overflow menu carries only what a reader wants on a phone. Edit-Wiki
  // and Copy-badge are maintainer chores and are dropped at that width
  // entirely, rather than crowding the menu.
  it("offers only the reader-facing actions in the overflow menu", async () => {
    const user = userEvent.setup();
    render(
      <WikiTopBar
        repo={SLUG}
        platform="gitea"
        badgePageId="overview"
        showEditWiki
        showSettings
      />,
    );
    await user.click(screen.getByRole("button", { name: /more wiki actions/i }));
    expect(await screen.findByRole("menuitem", { name: /open graph/i })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /wiki settings/i })).toBeInTheDocument();
    expect(screen.queryByRole("menuitem", { name: /edit wiki/i })).toBeNull();
    expect(screen.queryByRole("menuitem", { name: /copy readme badge/i })).toBeNull();
  });

  it("renders the repo as a linked button carrying the provider glyph", () => {
    render(<WikiTopBar repo={SLUG} platform="gitea" />);
    const link = screen.getByRole("link", { name: /widgets/i });
    expect(link).toHaveAttribute("href", "https://git.example.com/acme/widgets");
    expect(link).toHaveAttribute("target", "_blank");
    // Full slug ≥ md, owner/repo below — both present, one visible per width.
    expect(screen.getByText(SLUG)).toBeInTheDocument();
    expect(screen.getByText("acme/widgets")).toBeInTheDocument();
  });

  it("keeps a text label on the Copy-link CTA at both widths", () => {
    render(<WikiTopBar repo={SLUG} platform="gitea" />);
    // Full label ≥ md, shortened (never icon-only) below.
    expect(screen.getByText("Copy link")).toBeInTheDocument();
    expect(screen.getByText("Copy")).toBeInTheDocument();
  });
});
