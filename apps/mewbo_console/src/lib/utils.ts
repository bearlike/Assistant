import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

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
