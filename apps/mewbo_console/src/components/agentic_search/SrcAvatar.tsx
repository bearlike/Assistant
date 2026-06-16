import { cn } from "@/lib/utils"
import type { SourceCatalogEntry } from "../../types/agenticSearch"
import { sourceBrand } from "./sourceBrand"

interface SrcAvatarProps {
  source: SourceCatalogEntry | undefined
  size?: number
  className?: string
}

export function SrcAvatar({ source, size = 22, className }: SrcAvatarProps) {
  if (!source) return null
  // Known servers (github, gitea, searxng, huggingface, deepwiki/context7…)
  // render their official CC0 brand mark over a brand-tinted tile; everything
  // else falls back to the catalog's own letter glyph. Brand colors are an
  // external services' identity, so inline style (not a CSS-var token) is the
  // correct primitive here — same stance as the wiki PlatformIcon tiles.
  const brand = sourceBrand(source.id)
  const radius = Math.max(4, size * 0.22)
  if (brand) {
    return (
      <span
        aria-hidden
        className={cn("inline-flex items-center justify-center flex-none select-none", className)}
        style={{
          width: size,
          height: size,
          background: `${brand.hex}1f`, // ~12% tint of the brand color
          color: brand.hex,
          borderRadius: radius,
        }}
      >
        <svg
          viewBox="0 0 24 24"
          width={size * 0.62}
          height={size * 0.62}
          role="img"
          aria-label={brand.title}
        >
          <path d={brand.path} fill="currentColor" />
        </svg>
      </span>
    )
  }
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
        borderRadius: radius,
      }}
    >
      {source.glyph}
    </span>
  )
}
