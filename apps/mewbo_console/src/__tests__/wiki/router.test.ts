/**
 * Tests for the wiki route parser/builder — specifically the idempotent
 * ``?answer=<id>`` plumbing on the ``qa`` variant. The load-bearing invariant
 * is a clean parse↔build round-trip: a QA url that carries a persisted answer
 * id must survive `buildHref` → `parseWikiRoute` unchanged, so a refresh of a
 * shared link re-reads the saved answer instead of POSTing a fresh run.
 */
import { describe, expect, it } from "vitest";

import { buildHref, parseWikiRoute, type WikiRoute } from "@/components/wiki/router";

/** Split a built href into the (path, queryString) pair `parseWikiRoute` wants. */
function splitHref(href: string): [string, string] {
  const qIdx = href.indexOf("?");
  return qIdx === -1 ? [href, ""] : [href.slice(0, qIdx), href.slice(qIdx + 1)];
}

describe("parseWikiRoute — qa answer param", () => {
  it("parses the answer id off a qa url", () => {
    const route = parseWikiRoute(
      "/wiki/qa",
      "q=how%20does%20it%20work&page=core&answer=ans_123",
    );
    expect(route.kind).toBe("qa");
    if (route.kind !== "qa") return;
    expect(route.answer).toBe("ans_123");
    expect(route.question).toBe("how does it work");
    expect(route.pageId).toBe("core");
  });

  it("leaves answer undefined when the param is absent", () => {
    const route = parseWikiRoute("/wiki/qa", "q=hi&page=core");
    expect(route.kind).toBe("qa");
    if (route.kind !== "qa") return;
    expect(route.answer).toBeUndefined();
  });
});

describe("buildHref — qa answer param", () => {
  it("emits answer= when the route carries an id", () => {
    const href = buildHref({
      kind: "qa",
      question: "hi",
      pageId: "core",
      answer: "ans_123",
    });
    expect(href).toContain("answer=ans_123");
  });

  it("omits answer= when the route has no id", () => {
    const href = buildHref({ kind: "qa", question: "hi", pageId: "core" });
    expect(href).not.toContain("answer=");
  });
});

describe("qa route round-trip", () => {
  it("preserves answer alongside question / page / slug / model", () => {
    const route: WikiRoute = {
      kind: "qa",
      question: "what is the engine?",
      pageId: "architecture",
      slug: "owner/repo",
      model: "anthropic/claude-sonnet-4-5",
      answer: "ans_abc-789",
    };
    const [path, query] = splitHref(buildHref(route));
    expect(parseWikiRoute(path, query)).toEqual(route);
  });

  it("round-trips a qa url with no answer id (answer stays absent)", () => {
    const route: WikiRoute = {
      kind: "qa",
      question: "hello",
      pageId: "core",
      slug: "owner/repo",
    };
    const [path, query] = splitHref(buildHref(route));
    const parsed = parseWikiRoute(path, query);
    expect(parsed.kind).toBe("qa");
    if (parsed.kind !== "qa") return;
    expect(parsed.answer).toBeUndefined();
    expect(parsed).toMatchObject(route);
  });
});
