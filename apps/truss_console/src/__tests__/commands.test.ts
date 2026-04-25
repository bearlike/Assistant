import { describe, expect, it } from "vitest";
import { parseCommandInput } from "../lib/commands";

describe("parseCommandInput", () => {
  it("returns null for non-command text", () => {
    expect(parseCommandInput("hello world")).toBeNull();
    expect(parseCommandInput("")).toBeNull();
    expect(parseCommandInput("   ")).toBeNull();
  });

  it("parses bare commands", () => {
    expect(parseCommandInput("/help")).toEqual({ name: "help", args: [] });
    expect(parseCommandInput("/compact")).toEqual({
      name: "compact",
      args: [],
    });
  });

  it("parses commands with whitespace-separated args", () => {
    expect(parseCommandInput("/tag release-candidate")).toEqual({
      name: "tag",
      args: ["release-candidate"],
    });
    expect(parseCommandInput("/fork rc-1 --label foo")).toEqual({
      name: "fork",
      args: ["rc-1", "--label", "foo"],
    });
  });

  it("trims surrounding whitespace and lowercases the name", () => {
    expect(parseCommandInput("  /HELP  ")).toEqual({
      name: "help",
      args: [],
    });
  });

  it("returns the partial command name while typing", () => {
    expect(parseCommandInput("/co")).toEqual({ name: "co", args: [] });
  });
});
