import * as React from "react"
import { ArrowUp, Loader2 } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * Composer kit — the ONE shared visual primitive behind both the Tasks-page
 * composer (`InputComposerBody`) and the Agentic Search `SearchBar` (#82).
 *
 * The two composers were visually aligned by design but hand-rolled separately:
 * the same bordered surface, focus-within halo, attach/voice/maximize icon
 * buttons, and a send button. This kit owns that shared chrome so neither page
 * re-derives it. Honest extraction — VISUAL vocabulary only; each page keeps
 * its own state machine (Tasks: MCP/skills/worktrees/steering; Search:
 * workspace/tier/suggestions). No page-specific knobs leak in here (YAGNI).
 */

/** Surface + focus-within halo + elevation, shared by every composer surface.
 *  Spelled as a function of a few size knobs so a hero bar (2xl, elev-2) and a
 *  pill bar (full, elev-1) read from one source.
 *
 *  Focus treatment is aligned with the Tasks-page composer (`.composer-shell`):
 *  a PRIMARY-tinted halo + border on focus, so the two composers read as one
 *  family. The bloom (halo elevation + halo ring + border tint) is driven by
 *  the `.composer-surface` class in `index.css` so it transitions in 200ms
 *  ease-out and is killed under `prefers-reduced-motion` — matching the
 *  Tasks composer's `transition: box-shadow/border-color 200ms ease-out`. */
// eslint-disable-next-line react-refresh/only-export-components
export function composerSurface(opts: {
  radius: "rounded-2xl" | "rounded-xl" | "rounded-full"
  elevation: "elev-1" | "elev-2"
  /** Stronger halo on the hero surface, lighter on the pill/compact bars. */
  halo: "soft" | "strong"
}): string {
  return cn(
    // `.composer-surface` (index.css) owns the bordered card chrome + the
    // focus-within primary halo/elevation bloom + the 200ms transition +
    // reduced-motion guard. The Tailwind classes here only pick the
    // per-surface radius, base elevation, and halo strength via data-attrs.
    "composer-surface bg-[hsl(var(--card))]",
    opts.radius,
    `shadow-[var(--${opts.elevation})]`,
  )
}

/** Maps the `composerSurface` knobs to the `data-*` attribute that
 *  `.composer-surface` in `index.css` reads. Spread onto the surface element
 *  alongside `composerSurface(opts)` so the CSS focus bloom knows which halo
 *  strength to bloom to (the base elevation is already baked into the shadow
 *  class, so only `halo` needs to reach the CSS). */
// eslint-disable-next-line react-refresh/only-export-components
export function composerSurfaceData(opts: {
  halo: "soft" | "strong"
}): { "data-halo": string } {
  return { "data-halo": opts.halo }
}

/** Shared icon-button atom (attach / voice / maximize). Square, ghost, themed
 *  hover — the identical treatment both composers used inline. */
export const ComposerIconButton = React.forwardRef<
  HTMLButtonElement,
  React.ButtonHTMLAttributes<HTMLButtonElement> & { size?: number }
>(function ComposerIconButton({ className, size = 30, children, type, ...rest }, ref) {
  return (
    <button
      ref={ref}
      type={type ?? "button"}
      style={{ height: size, width: size }}
      className={cn(
        "rounded-md inline-flex items-center justify-center transition-colors flex-none",
        "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--accent))] hover:text-[hsl(var(--foreground))]",
        className,
      )}
      {...rest}
    >
      {children}
    </button>
  )
})

export interface ComposerSendButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  /** A submission is in flight — shows the spinner and disables. */
  submitting?: boolean
  /** The input has content worth sending — drives the primary/idle palette. */
  active: boolean
  /** Visual shape: `round` (hero/default) or `square` (compact). */
  shape?: "round" | "square"
  /** Icon when idle; defaults to the up-arrow. */
  icon?: React.ReactNode
}

/** Shared send button — primary clay when active, muted when empty, spinner
 *  while submitting. Both composers used this exact branch. */
export const ComposerSendButton = React.forwardRef<HTMLButtonElement, ComposerSendButtonProps>(
  function ComposerSendButton(
    { submitting = false, active, shape = "round", icon, className, disabled, type, ...rest },
    ref,
  ) {
    const square = shape === "square"
    return (
      <button
        ref={ref}
        type={type ?? "button"}
        disabled={disabled ?? (!active || submitting)}
        className={cn(
          "inline-flex items-center justify-center transition-colors flex-none",
          square ? "h-7 w-7 rounded-md" : "h-9 w-9 rounded-full",
          active && !submitting
            ? "bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:opacity-90"
            : "bg-[hsl(var(--muted))] text-[hsl(var(--muted-foreground))] cursor-not-allowed",
          className,
        )}
        {...rest}
      >
        {submitting ? (
          <Loader2 className={cn("animate-spin", square ? "h-3.5 w-3.5" : "h-4 w-4")} />
        ) : (
          icon ?? <ArrowUp className={square ? "h-3.5 w-3.5" : "h-4 w-4"} />
        )}
      </button>
    )
  },
)

export interface ComposerShellProps {
  /** Outer ref — for outside-click / focus targeting by the consumer. */
  wrapRef?: React.Ref<HTMLDivElement>
  /** Surface shape/elevation/halo knobs (see `composerSurface`). */
  surface: Parameters<typeof composerSurface>[0]
  /** Inner padding for the surface body. */
  bodyClassName?: string
  /** Content above the toolbar — typically the input/textarea region (plus any
   *  run indicator / attached-file chip). */
  top?: React.ReactNode
  /** Extra content between `top` and the toolbar (optional). */
  children?: React.ReactNode
  /** Left toolbar slot (attach + page-specific chips). */
  toolbarLeft?: React.ReactNode
  /** Right toolbar slot (voice + send). */
  toolbarRight?: React.ReactNode
  /** Suggestion popover, absolutely positioned below the surface by the shell. */
  popover?: React.ReactNode
  className?: string
}

/**
 * Slotted composer surface: `top` → input (`children`) → toolbar (left/right) →
 * anchored `popover`. The shell owns the surface chrome + relative positioning
 * for the dropdown; the consumer supplies the input element and the toolbar
 * contents. Used by `SearchBar`; available to any future composer.
 */
export function ComposerShell({
  wrapRef,
  surface,
  bodyClassName,
  top,
  children,
  toolbarLeft,
  toolbarRight,
  popover,
  className,
}: ComposerShellProps) {
  return (
    <div ref={wrapRef} className={cn("relative w-full", className)}>
      <div
        className={cn(composerSurface(surface), bodyClassName)}
        {...composerSurfaceData(surface)}
      >
        {top}
        {children}
        {(toolbarLeft || toolbarRight) && (
          <div className="flex items-center justify-between gap-2">
            <div className="inline-flex items-center gap-1 min-w-0">{toolbarLeft}</div>
            <div className="inline-flex items-center gap-1 flex-none">{toolbarRight}</div>
          </div>
        )}
      </div>
      {popover}
    </div>
  )
}
