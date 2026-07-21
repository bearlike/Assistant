/**
 * End-to-end cover for inline `src:` citations: a wiki page body is markdown,
 * so the chip only reaches the reader if it survives react-markdown's URL
 * sanitizer. `defaultUrlTransform` blanks every scheme outside
 * http(s)/mailto/irc/xmpp, which silently reduced `[label](src:path#L1-9)` to
 * an empty-href anchor carrying the raw label — the chip branch in
 * `buildMarkdownComponents` was unreachable from real content.
 *
 * These tests render through `ReactMarkdown` (not `SrcChip` directly) because
 * that is the seam that was broken: a unit test on the chip passes either way.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import ReactMarkdown from "react-markdown";

import {
  SourceHrefProvider,
  buildMarkdownComponents,
  wikiUrlTransform,
} from "@/components/wiki/markdownComponents";
import { MarkdownBlock } from "@/components/wiki/MarkdownBlock";

afterEach(cleanup);

// `UrlTransform` receives the hast node; the transform ignores it, so a bare
// element stands in for the direct-call assertions below.
const NODE = { type: "element", tagName: "a", properties: {}, children: [] } as never;

/** Internal-page navigation is irrelevant here — these bodies cite files. */
function noNav(): void {
  return;
}

function renderBody(body: string, href: string | null = "https://git.example.com/o/r/src/branch/main/pyproject.toml#L63-L76") {
  const components = buildMarkdownComponents({ onNavigatePage: noNav });
  return render(
    <SourceHrefProvider resolve={() => href} platform="gitea">
      <ReactMarkdown urlTransform={wikiUrlTransform} components={components}>
        {body}
      </ReactMarkdown>
    </SourceHrefProvider>,
  );
}

describe("inline src: citations through react-markdown", () => {
  it("renders a badge (not a bare anchor) for a scheme-prefixed citation", () => {
    renderBody("[pyproject.toml L63-76](src:pyproject.toml#L63-76)");

    // The badge splits path and range into its two cells — the raw markdown
    // label is replaced, which is how we know the chip branch was reached.
    expect(screen.getByText("pyproject.toml")).toBeInTheDocument();
    expect(screen.getByText("L63–76")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Gitea" })).toBeInTheDocument();

    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "https://git.example.com/o/r/src/branch/main/pyproject.toml#L63-L76",
    );
    expect(link.getAttribute("href")).not.toBe("");
  });

  it("keeps the badge for a nested path citation", () => {
    renderBody("see [x](src:apps/mewbo_api/src/mewbo_api/backend.py#L10-L20)");
    expect(screen.getByText("apps/mewbo_api/src/mewbo_api/backend.py")).toBeInTheDocument();
    expect(screen.getByText("L10–20")).toBeInTheDocument();
  });

  // The "Relevant source files" accordion used to render dead text while its
  // sibling Sources footer rendered chips — same data shape, two treatments.
  it("renders the relevant-sources accordion as clickable badges", () => {
    render(
      <SourceHrefProvider
        resolve={(c) =>
          `https://git.example.com/o/r/src/commit/deadbeef/${c.path}`
        }
        platform="gitea"
      >
        <MarkdownBlock
          body=""
          frontmatter={{
            title: "Overview",
            slug: "overview",
            relevantSources: [{ path: "pyproject.toml", lines: "L63-76" }],
          }}
          onNavigatePage={noNav}
          onZoomDiagram={noNav}
        />
      </SourceHrefProvider>,
    );
    const link = screen.getByRole("link");
    expect(link).toHaveAttribute(
      "href",
      "https://git.example.com/o/r/src/commit/deadbeef/pyproject.toml",
    );
    expect(screen.getByText("pyproject.toml")).toBeInTheDocument();
    expect(screen.getByText("L63–76")).toBeInTheDocument();
    expect(screen.getByRole("img", { name: "Gitea" })).toBeInTheDocument();
  });

  it("still sanitizes genuinely unsafe schemes", () => {
    expect(wikiUrlTransform("javascript:alert(1)", "href", NODE)).toBe("");
    expect(wikiUrlTransform("https://example.com/a", "href", NODE)).toBe(
      "https://example.com/a",
    );
    expect(wikiUrlTransform("src:a/b.py#L1-2", "href", NODE)).toBe("src:a/b.py#L1-2");
  });
});
