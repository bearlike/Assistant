/**
 * resultMeta — meta-map → typed chip classification + formatting.
 *
 * The card renders structured per-result facts from an open-vocab `meta` map.
 * These pin the formatting contract: count-ish keys → compact numbers, date-ish
 * → relative time, everything else → a labelled tag chip.
 */
import { describe, expect, it } from "vitest"

import { compactNumber, formatBytes, labelize, metaChip, metaChips, statusTone } from "./resultMeta"

describe("compactNumber", () => {
  it("leaves small numbers alone", () => {
    expect(compactNumber(0)).toBe("0")
    expect(compactNumber(999)).toBe("999")
  })
  it("compacts thousands/millions/billions", () => {
    expect(compactNumber(46_200)).toBe("46.2k")
    expect(compactNumber(1_500_000)).toBe("1.5M")
    expect(compactNumber(2_000_000_000)).toBe("2B")
  })
  it("drops a trailing .0", () => {
    expect(compactNumber(46_000)).toBe("46k")
    expect(compactNumber(3_000)).toBe("3k")
  })
})

describe("formatBytes", () => {
  it("renders bytes / KB / MB with one decimal, dropping a trailing .0", () => {
    expect(formatBytes(512)).toBe("512 B")
    expect(formatBytes(1536)).toBe("1.5 KB")
    expect(formatBytes(24_576)).toBe("24 KB")
    expect(formatBytes(5_242_880)).toBe("5 MB")
  })
})

describe("statusTone", () => {
  it("maps known values, normalizing separators/case, else neutral", () => {
    expect(statusTone("Open")).toBe("positive")
    expect(statusTone("IN_PROGRESS")).toBe("positive")
    expect(statusTone("merged")).toBe("done")
    expect(statusTone("failed")).toBe("negative")
    expect(statusTone("Draft")).toBe("pending")
    expect(statusTone("whatever")).toBe("neutral")
  })
})

describe("labelize", () => {
  it("title-cases snake/kebab keys", () => {
    expect(labelize("sub_issues")).toBe("Sub issues")
    expect(labelize("open-state")).toBe("Open state")
  })
})

describe("metaChip classification", () => {
  it("count-ish keys get an icon + compact number", () => {
    const c = metaChip("stars", 46_200)
    expect(c.kind).toBe("count")
    expect(c.value).toBe("46.2k")
    expect(c.Icon).toBeTruthy()
  })

  it("numeric STRING on a count key still compacts", () => {
    const c = metaChip("downloads", "12400")
    expect(c.kind).toBe("count")
    expect(c.value).toBe("12.4k")
  })

  it("ISO date values render as relative time", () => {
    const c = metaChip("updated", "2020-01-01T00:00:00Z")
    expect(c.kind).toBe("time")
    // RelativeTime returns "N years ago" — just assert it transformed the ISO.
    expect(c.value).not.toBe("2020-01-01T00:00:00Z")
    expect(c.value.length).toBeGreaterThan(0)
  })

  it("version strings stay plain tag chips (not mistaken for a date)", () => {
    const c = metaChip("version", "1.2.3")
    expect(c.kind).toBe("tag")
    expect(c.value).toBe("1.2.3")
  })

  it("language/license are labelled tag chips", () => {
    expect(metaChip("language", "Python").kind).toBe("tag")
    expect(metaChip("license", "MIT").kind).toBe("tag")
  })

  it("state/status become colour-coded status badges (value title-cased)", () => {
    const open = metaChip("state", "open")
    expect(open.kind).toBe("status")
    expect(open.tone).toBe("positive")
    expect(open.value).toBe("Open") // labelized for display
    expect(metaChip("status", "merged").tone).toBe("done")
    expect(metaChip("status", "cancelled").tone).toBe("negative")
    expect(metaChip("status", "in_progress").value).toBe("In progress")
    expect(metaChip("status", "in_progress").tone).toBe("positive")
    // An unknown status still renders as a (neutral) badge — never dropped.
    expect(metaChip("state", "mystery").tone).toBe("neutral")
  })

  it("byte-size keys humanize to a size chip", () => {
    const c = metaChip("size", 24_576)
    expect(c.kind).toBe("count")
    expect(c.value).toBe("24 KB")
    expect(metaChip("file_size", 1536).value).toBe("1.5 KB")
    // `team_size` is NOT a byte key — it stays an unlabelled tag count fallback.
    expect(metaChip("team_size", 12).kind).not.toBe("count")
  })

  it("unknown keys become label: value tag chips", () => {
    const c = metaChip("custom_field", "hello")
    expect(c.kind).toBe("tag")
    expect(c.label).toBe("Custom field")
    expect(c.value).toBe("hello")
  })

  it("booleans render the label / negation", () => {
    expect(metaChip("archived", true).value).toBe("Archived")
    expect(metaChip("archived", false).value).toBe("No archived")
  })
})

describe("metaChips", () => {
  it("returns [] for null/undefined meta", () => {
    expect(metaChips(null)).toEqual([])
    expect(metaChips(undefined)).toEqual([])
  })
  it("preserves insertion order", () => {
    const chips = metaChips({ stars: 10, language: "Go", forks: 2 })
    expect(chips.map((c) => c.key)).toEqual(["stars", "language", "forks"])
  })
})
