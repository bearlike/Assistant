/**
 * Render rules for a result card's structured `meta` map — the card's footer.
 *
 * `meta` IS the open-vocab "card metadata" the emitting agent attaches: GitHub
 * repo stats, an issue's state + assignee, a HF model's downloads, a doc's size
 * + last-updated. There is ONE such map per result (no parallel `card_meta`
 * field — the snippet stays prose, every quantitative/enumerable fact rides
 * here). We don't know the keys ahead of time, so this module classifies a
 * `key: value` pair into a typed chip the card's footer renders consistently:
 *
 *  - count-ish keys (stars/forks/downloads/likes/citations/issues/comments/…)
 *    → an icon + a COMPACT number (`46.2k`)
 *  - size-ish keys (size/bytes/filesize/…) → a humanized BYTE size (`24 KB`)
 *  - date-ish keys/values (updated/created/…ISO) → relative time
 *  - state/status → a colour-coded STATUS badge (open=green, merged/closed=blue,
 *    failed=red, draft=amber, else neutral)
 *  - language/license/version → a plain labelled chip
 *  - anything else → a `label: value` chip
 *
 * Pure + unit-testable; the card maps over `metaChips(meta)`.
 */
import {
  CircleDot,
  Clock,
  Download,
  FileText,
  GitFork,
  Heart,
  ListTree,
  MessageSquare,
  Quote,
  Scale,
  Star,
  Tag,
  type LucideIcon,
} from "lucide-react"

import { RelativeTime } from "../wiki/relativeTime"

export type MetaChipKind = "count" | "time" | "tag" | "status"

/**
 * Semantic tone of a `status`/`state` value — drives the badge colour.
 * The card maps each tone onto a CSS theme token (never a hardcoded colour).
 */
export type StatusTone = "positive" | "done" | "negative" | "pending" | "neutral"

export interface MetaChip {
  key: string
  /** Display label (title-cased key). */
  label: string
  /** Formatted value (compact number, byte size, relative time, or raw string). */
  value: string
  kind: MetaChipKind
  /** Icon for count chips (and a few tag chips); undefined → no icon. */
  Icon?: LucideIcon
  /** Colour tone for `status` chips; undefined for every other kind. */
  tone?: StatusTone
}

// Count-ish keys → icon. Matched against the normalized key.
const COUNT_ICONS: ReadonlyArray<readonly [string, LucideIcon]> = [
  ["stars", Star],
  ["star", Star],
  ["forks", GitFork],
  ["fork", GitFork],
  ["downloads", Download],
  ["download", Download],
  ["likes", Heart],
  ["like", Heart],
  ["citations", Quote],
  ["citation", Quote],
  ["sub_issues", ListTree],
  ["subissues", ListTree],
  ["sub_tasks", ListTree],
  ["issues", CircleDot],
  ["comments", MessageSquare],
  ["comment", MessageSquare],
  ["watchers", Star],
]

// Tag-ish keys with a recognizable icon (otherwise the chip is icon-less).
// `state`/`status` are NOT here — they classify as a colour-coded status badge.
const TAG_ICONS: ReadonlyArray<readonly [string, LucideIcon]> = [
  ["language", Tag],
  ["license", Scale],
  ["version", Tag],
  ["priority", CircleDot],
]

const DATE_KEYS = ["updated", "created", "modified", "date", "published", "merged", "closed"]

// Byte-size keys → a humanized size (`24 KB`). `size` alone is the doc-size case
// the playbooks teach; `team_size` etc. stay counts (they aren't an exact match
// and don't carry a byte-ish token).
function isSizeKey(normKey: string): boolean {
  return (
    normKey === "size" ||
    normKey === "bytes" ||
    normKey.includes("filesize") ||
    normKey.includes("file_size") ||
    normKey.includes("disk")
  )
}

/** Humanize a byte count: 1536 → "1.5 KB", 24_576 → "24 KB". */
export function formatBytes(n: number): string {
  if (!Number.isFinite(n)) return String(n)
  if (Math.abs(n) < 1024) return `${n} B`
  const units = ["KB", "MB", "GB", "TB"]
  let val = n / 1024
  let i = 0
  while (Math.abs(val) >= 1024 && i < units.length - 1) {
    val /= 1024
    i += 1
  }
  return `${val.toFixed(1).replace(/\.0$/, "")} ${units[i]}`
}

// status VALUE (normalized) → tone. Unmapped values fall back to "neutral", so
// any `state`/`status` still renders as a badge — only the colour is unknown.
const STATUS_TONE_BY_VALUE: ReadonlyArray<readonly [StatusTone, readonly string[]]> = [
  ["positive", ["open", "active", "ongoing", "in progress", "reopened", "live", "available", "running", "passing", "enabled", "new"]],
  ["done", ["merged", "done", "resolved", "completed", "complete", "closed", "fixed", "shipped", "deployed", "approved", "succeeded", "success"]],
  ["negative", ["failed", "failure", "error", "rejected", "cancelled", "canceled", "blocked", "broken", "declined", "deprecated", "disabled", "overdue", "abandoned"]],
  ["pending", ["draft", "wip", "pending", "in review", "review", "queued", "waiting", "backlog", "todo", "to do", "scheduled", "paused", "on hold"]],
]

/** Map a status/state value to a colour tone (exact match on normalized value). */
export function statusTone(value: string): StatusTone {
  const norm = value.toLowerCase().replace(/[\s_-]+/g, " ").trim()
  for (const [tone, values] of STATUS_TONE_BY_VALUE) {
    if (values.includes(norm)) return tone
  }
  return "neutral"
}

function normalizeKey(key: string): string {
  return key.toLowerCase().replace(/[\s-]+/g, "_")
}

/** Title-case a snake/kebab key for display: `sub_issues` → `Sub issues`. */
export function labelize(key: string): string {
  const spaced = key.replace(/[_-]+/g, " ").trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/** Compact a number like 46_200 → "46.2k", 1_500_000 → "1.5M". */
export function compactNumber(n: number): string {
  if (!Number.isFinite(n)) return String(n)
  const abs = Math.abs(n)
  if (abs < 1000) return String(n)
  const units: [number, string][] = [
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "k"],
  ]
  for (const [scale, suffix] of units) {
    if (abs >= scale) {
      const scaled = n / scale
      // One decimal, but drop a trailing ".0" (46.0k → 46k).
      const text = scaled.toFixed(1).replace(/\.0$/, "")
      return `${text}${suffix}`
    }
  }
  return String(n)
}

const ISO_LIKE = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?/

function iconFor(
  table: ReadonlyArray<readonly [string, LucideIcon]>,
  normKey: string
): LucideIcon | undefined {
  for (const [needle, Icon] of table) {
    if (normKey.includes(needle)) return Icon
  }
  return undefined
}

/** Classify a single `key: value` pair into a typed, formatted chip. */
export function metaChip(key: string, value: string | number | boolean): MetaChip {
  const normKey = normalizeKey(key)
  const label = labelize(key)

  const numeric =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim() !== "" && !Number.isNaN(Number(value))
      ? Number(value)
      : null

  // Byte-size key + a number → a humanized size chip (`24 KB`).
  if (isSizeKey(normKey) && numeric != null) {
    return { key, label, value: formatBytes(numeric), kind: "count", Icon: FileText }
  }

  // Numbers (and numeric strings) on a count-ish key → compact count chip.
  const countIcon = iconFor(COUNT_ICONS, normKey)
  if (countIcon && numeric != null) {
    return { key, label, value: compactNumber(numeric), kind: "count", Icon: countIcon }
  }

  // A state/status string → a colour-coded status badge (the ticket/PR signal).
  if (typeof value === "string" && (normKey.includes("state") || normKey.includes("status"))) {
    return { key, label, value: labelize(value), kind: "status", tone: statusTone(value) }
  }

  // Date-ish key OR an ISO-looking string value → relative time.
  const isDateKey = DATE_KEYS.some((k) => normKey.includes(k))
  if (typeof value === "string" && (isDateKey || ISO_LIKE.test(value))) {
    const rel = RelativeTime.format(value)
    return { key, label, value: rel || value, kind: "time", Icon: Clock }
  }

  // Booleans render as the label when true, "no <label>" when false.
  if (typeof value === "boolean") {
    return { key, label, value: value ? label : `No ${label.toLowerCase()}`, kind: "tag" }
  }

  // Plain tag chip (language/license/version) or a labelled fallback.
  const tagIcon = iconFor(TAG_ICONS, normKey)
  return { key, label, value: String(value), kind: "tag", Icon: tagIcon }
}

/** Map a result's `meta` into ordered chips (insertion order preserved). */
export function metaChips(
  meta: Record<string, string | number | boolean> | null | undefined
): MetaChip[] {
  if (!meta) return []
  return Object.entries(meta).map(([k, v]) => metaChip(k, v))
}
