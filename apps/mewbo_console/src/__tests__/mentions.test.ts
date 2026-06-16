import { describe, expect, it } from "vitest";
import { parseMentionInput, spliceMention } from "../lib/mentions";

describe("parseMentionInput", () => {
  it("returns null when there is no @ before the caret", () => {
    expect(parseMentionInput("hello world", 11)).toBeNull();
    expect(parseMentionInput("", 0)).toBeNull();
  });

  it("detects a bare @ at the start of the text", () => {
    expect(parseMentionInput("@", 1)).toEqual({ query: "", start: 0 });
  });

  it("detects an @token at the start of the text", () => {
    expect(parseMentionInput("@src/app", 8)).toEqual({
      query: "src/app",
      start: 0,
    });
  });

  it("detects an @token after whitespace", () => {
    // caret at end of "look at @main.ts"
    const text = "look at @main.ts";
    expect(parseMentionInput(text, text.length)).toEqual({
      query: "main.ts",
      start: 8,
    });
  });

  it("ignores email-style @ (preceded by a word char)", () => {
    const text = "ping me at krishna@gmail.com please";
    // caret right after "gmail.com"
    expect(parseMentionInput(text, "ping me at krishna@gmail.com".length)).toBeNull();
  });

  it("returns null once whitespace separates the @ from the caret", () => {
    const text = "@src/app and more";
    // caret after "more" — a space sits between the token and the caret
    expect(parseMentionInput(text, text.length)).toBeNull();
  });

  it("parses the active token mid-string when the caret is inside it", () => {
    const text = "see @src/comp here";
    // caret right after "@src/comp"
    const caret = "see @src/comp".length;
    expect(parseMentionInput(text, caret)).toEqual({
      query: "src/comp",
      start: 4,
    });
  });

  it("only reads up to the caret, not the whole token", () => {
    const text = "@src/app";
    // caret after "@sr"
    expect(parseMentionInput(text, 3)).toEqual({ query: "sr", start: 0 });
  });

  it("clamps an out-of-range caret", () => {
    expect(parseMentionInput("@a", 99)).toEqual({ query: "a", start: 0 });
  });
});

describe("spliceMention", () => {
  it("replaces the active @query with @path and a trailing space", () => {
    const text = "@src";
    const out = spliceMention(text, 4, { query: "src", start: 0 }, "src/app.ts");
    expect(out.value).toBe("@src/app.ts ");
    expect(out.caret).toBe("@src/app.ts ".length);
  });

  it("preserves text before and after the token", () => {
    const text = "look at @ma then stop";
    const caret = "look at @ma".length;
    const out = spliceMention(text, caret, { query: "ma", start: 8 }, "main.ts");
    // The token's trailing space subsumes the existing " then" leading space,
    // so there's exactly one space between the ref and the following word.
    expect(out.value).toBe("look at @main.ts then stop");
    // caret sits just after the inserted "@main.ts "
    expect(out.value.slice(0, out.caret)).toBe("look at @main.ts ");
  });

  it("does not double a space the user already typed after the caret", () => {
    const text = "@src extra";
    const caret = 4; // right after "@src", before the space
    const out = spliceMention(text, caret, { query: "src", start: 0 }, "src/app.ts");
    // the existing leading space of " extra" is consumed by the token's space
    expect(out.value).toBe("@src/app.ts extra");
  });
});
