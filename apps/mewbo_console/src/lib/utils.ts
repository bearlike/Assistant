import { clsx, type ClassValue } from "clsx"
import { extendTailwindMerge } from "tailwind-merge"

/**
 * tailwind-merge ships a hardcoded list of Tailwind's stock class groups, so a
 * font size added via `theme.extend.fontSize` is invisible to it. `text-field`
 * (our 16px input floor) was therefore mis-classified as a text COLOUR, with
 * two silent consequences:
 *
 *   cn('text-[hsl(var(--foreground))]', 'text-field')  ->  colour DROPPED
 *   cn('text-field md:text-sm', 'text-xs')             ->  BOTH sizes survive
 *
 * The second is the dangerous one. When two size utilities reach the DOM
 * together, stylesheet order decides — and Tailwind emits fontSize rules
 * ALPHABETICALLY, not by value, so `text-sm`/`text-xs` happen to beat
 * `text-field` while `text-2xs`/`text-base` happen to lose to it. That is
 * arbitrary, invisible at the call site, and it was live: a `text-xs` passed
 * into an input already carrying the floor won, defeating it on exactly the
 * narrow viewports the floor exists to protect.
 *
 * Registering the key restores normal last-wins semantics and stops it being
 * read as a colour. Any future `theme.extend.fontSize` key must be added here
 * too — the extension is not automatic.
 */
const twMerge = extendTailwindMerge({
  extend: { classGroups: { "font-size": ["text-field"] } },
})

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
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
