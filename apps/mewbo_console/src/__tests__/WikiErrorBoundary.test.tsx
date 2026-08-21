import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WikiErrorBoundary } from "@/components/WikiErrorBoundary";

afterEach(cleanup);

function BrokenWikiScreen(): never {
  throw new TypeError("Cannot render wiki page");
}

describe("WikiErrorBoundary", () => {
  it("keeps the surrounding app mounted when a wiki screen throws", () => {
    const consoleError = vi.spyOn(console, "error").mockImplementation(() => undefined);

    try {
      render(
        <div>
          <header>Console navigation</header>
          <WikiErrorBoundary>
            <BrokenWikiScreen />
          </WikiErrorBoundary>
        </div>,
      );

      expect(screen.getByText("Console navigation")).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(
        "This wiki screen failed to load",
      );
      expect(screen.getByText("Cannot render wiki page")).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /reload page/i })).toBeInTheDocument();
      expect(consoleError).toHaveBeenCalled();
    } finally {
      consoleError.mockRestore();
    }
  });
});
