import { cn } from "@/lib/utils"
import type { SourceCatalogEntry } from "../../types/agenticSearch"

interface SrcAvatarProps {
  source: SourceCatalogEntry | undefined
  size?: number
  className?: string
}

export function SrcAvatar({ source, size = 22, className }: SrcAvatarProps) {
  if (!source) return null
  // Brand-tinted; CSS-variable theme tokens don't apply (these are external
  // services' own brand colors). Inline style is the correct primitive here.
  return (
    <span
      aria-hidden
      className={cn(
        "inline-flex items-center justify-center font-semibold flex-none select-none",
        className
      )}
      style={{
        width: size,
        height: size,
        background: source.bg,
        color: source.color,
        fontSize: Math.max(9, size * 0.45),
        borderRadius: Math.max(4, size * 0.22),
      }}
    >
      {source.glyph}
    </span>
  )
}
