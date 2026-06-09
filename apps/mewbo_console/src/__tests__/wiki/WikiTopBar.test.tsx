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
