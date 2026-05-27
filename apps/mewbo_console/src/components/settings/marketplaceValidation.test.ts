/**
 * Tests for `validateMarketplaceEntry` — the client mirror of the backend
 * resolver `plugins.py::_resolve_git_url`. Every accepted/rejected branch is
 * covered, including the real-world examples the backend resolves.
 */
import { describe, expect, it } from "vitest";

import { validateMarketplaceEntry } from "./marketplaceValidation";

describe("validateMarketplaceEntry", () => {
  describe("full URLs (verbatim)", () => {
    it.each([
      "https://github.com/anthropics/claude-plugins-official.git",
      "http://example.com/owner/repo",
      "ssh://git@git.hurricane.home/bearlike/Assistant.git",
      "git://github.com/owner/repo.git",
      // Scheme matching is case-insensitive (mirrors `re.IGNORECASE`).
      "HTTPS://github.com/owner/repo",
    ])("accepts %s", (entry) => {
      expect(validateMarketplaceEntry(entry)).toBeNull();
    });
  });

  describe("scp-style SSH refs (verbatim)", () => {
    it.each([
      "git@github.com:owner/repo",
      "git@github.com:owner/repo.git",
      "git@git.hurricane.home:bearlike/Assistant.git",
    ])("accepts %s", (entry) => {
      expect(validateMarketplaceEntry(entry)).toBeNull();
    });
  });

  describe("host/owner/repo (host has a dot or port)", () => {
    it.each([
      "git.hurricane.home/bearlike/Assistant",
      "git.hurricane.home/bearlike/Assistant.git",
      "gitea.example.com/team/plugins",
      "localhost:3000/owner/repo",
    ])("accepts %s", (entry) => {
      expect(validateMarketplaceEntry(entry)).toBeNull();
    });

    it("rejects a 3-segment path whose first segment is not host-like", () => {
      // `foo` has no dot/port → not a host; and >2 segments → not bare repo.
      expect(validateMarketplaceEntry("foo/bar/baz")).not.toBeNull();
    });
  });

  describe("bare owner/repo (default host)", () => {
    it.each([
      "anthropics/claude-plugins-official",
      "owner/repo",
      "owner/repo.git",
    ])("accepts %s", (entry) => {
      expect(validateMarketplaceEntry(entry)).toBeNull();
    });

    it("trims surrounding whitespace before validating", () => {
      expect(validateMarketplaceEntry("  owner/repo  ")).toBeNull();
    });
  });

  describe("rejections", () => {
    it("rejects an empty string", () => {
      expect(validateMarketplaceEntry("")).toBe("Repository cannot be empty");
    });

    it("rejects whitespace-only", () => {
      expect(validateMarketplaceEntry("   ")).toBe("Repository cannot be empty");
    });

    it("rejects a single token (stricter than the backend)", () => {
      expect(validateMarketplaceEntry("foo")).toBe(
        "Use a git URL, host/owner/repo, or owner/repo"
      );
    });

    it("rejects a free-text string", () => {
      expect(validateMarketplaceEntry("not a url")).toBe(
        "Use a git URL, host/owner/repo, or owner/repo"
      );
    });

    it("rejects owner/ with an empty repo segment", () => {
      expect(validateMarketplaceEntry("owner/")).not.toBeNull();
    });

    it("rejects /repo with an empty owner segment", () => {
      expect(validateMarketplaceEntry("/repo")).not.toBeNull();
    });
  });
});
