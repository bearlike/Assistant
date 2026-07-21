import * as React from "react"
import { ArrowUp, Loader2 } from "lucide-react"

import { cn } from "@/lib/utils"

/**
 * Composer kit — the shared React surface for every composer that isn't the
 * Tasks body: the Agentic Search `SearchBar` and the siblings converging
 * onto `ComposerShell` (Wiki's QADock, Apps' HeroComposer).
 *
 * The CHROME family itself is shared even wider: `.composer-surface` +
 * `data-*` attributes (in `index.css`) own the bordered surface, the
 * focus-within halo, and the running/command tints for the Tasks composer TOO
 * — Tasks' `composerCard()` (`InputBar.tsx`) rides the same class and only
 * layers its own elevation/padding on top. So this kit is not "the composer
 * everyone else uses instead of Tasks"; it is the reusable body/toolbar
 * scaffolding, and the chrome is genuinely one system. Honest extraction —
 * VISUAL vocabulary only; each page keeps its own state machine (Tasks:
 * MCP/skills/worktrees/steering; Search: workspace/tier/suggestions). No
 * page-specific knobs leak in here (YAGNI).
 */

/** Surface + focus-within halo + elevation, shared by every composer surface.
 *  The radius is fixed (`--composer-radius`, the single composer radius knob);
 *  only base elevation and halo strength vary, so a hero bar (elev-2, strong)
 *  and a pill bar (elev-1, soft) read from one source.
 *
 *  Focus + running/command treatment is owned by the `.composer-surface` class
 *  in `index.css`: a PRIMARY-tinted halo + border on focus (permission-tinted
 *  while a run is live), so every composer reads as one family. The bloom (halo
 *  elevation + halo ring + border tint) transitions in 200ms ease-out and is
 *  killed under `prefers-reduced-motion`. */
// eslint-disable-next-line react-refresh/only-export-components
export function composerSurface(opts: {
  elevation: "elev-1" | "elev-2"
  /** Stronger halo on the hero surface, lighter on the pill/compact bars. */
  halo: "soft" | "strong"
}): string {
  return cn(
    // `.composer-surface` (index.css) owns the bordered card chrome + the
    // focus-within primary halo/elevation bloom + the 200ms transition +
    // reduced-motion guard. The Tailwind classes here only pick the base
    // elevation; the halo strength reaches the CSS via the `data-halo` attr.
    // `--composer-bg` is the single background knob (resolves to the card).
    // `[box-shadow:var(--x)]` is required here, NOT `shadow-[var(--x)]` — the
    // bare `shadow-[var(--…)]` prefix is ambiguous to Tailwind (parsed as
    // boxShadowColor) and renders no shadow at all.
    "composer-surface bg-[var(--composer-bg)] rounded-[var(--composer-radius)]",
    `[box-shadow:var(--${opts.elevation})]`,
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

/** The composer input's type size — the ONE place it lives, because the
 *  constraint behind it is easy to lose:
 *
 *  **Mobile Safari zooms the viewport whenever a focused field computes below
 *  16px.** A composer is the app's most-focused input, so its narrow-viewport
 *  size must come from `text-field` — a fixed 16px that does NOT move with the
 *  type scale. `text-base` is 14px here and would trip the zoom; it is the
 *  wrong class for an input even though it reads like the neutral default.
 *
 *  ⚠️ `text-field`, never `text-input`: `colors.input` is shadcn's border token,
 *  so a `fontSize.input` step would emit `.text-input` twice and the colour rule
 *  would win, painting typed text in the dim border colour. `ui/input.tsx`
 *  carries the full note.
 *
 *  `text-field` at every width, hero or compact — "hero" vs "compact" composer
 *  sizing lives in padding and chrome, never in the input's own font size (a
 *  `md:text-sm` step-down briefly existed here for the compact case and had
 *  zero callers; deleted rather than left as a trap for the next one).
 *
 *  **The placeholder is a SEPARATE, smaller tier** — `placeholder:text-base
 *  placeholder:font-normal` (14px, explicit normal weight). Typed text at 16px
 *  reads as instrument-panel-appropriate once it's live user content, but an
 *  at-rest placeholder at the same 16px read as oversized next to the rest of
 *  the 13–14px UI ("large fat text"). This is safe against the iOS zoom: the
 *  zoom keys off the FOCUSED INPUT's own computed size (still `text-field`,
 *  unconditionally, above), never the `::placeholder` pseudo-element's — a
 *  placeholder is inert display text, not a focus target. `font-normal` is
 *  explicit rather than relying on inheritance, so a weight change upstream
 *  can never silently bold the placeholder again.
 *
 *  Merge it FIRST (`cn(composerInputCls(), className)`) so a caller can still
 *  override. Every composer must get its placeholder tier from HERE — never
 *  spell `placeholder:text-*`/`placeholder:font-*` locally (the placeholder
 *  COLOUR law is a separate, pre-existing rule and stays local: bare
 *  `placeholder:text-[hsl(var(--muted-foreground))]`, no opacity modifier). */
// eslint-disable-next-line react-refresh/only-export-components
export function composerInputCls(): string {
  return "text-field placeholder:text-base placeholder:font-normal"
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
  /** Surface elevation/halo knobs (see `composerSurface`); the radius is fixed
   *  at `--composer-radius` for every composer. */
  surface: Parameters<typeof composerSurface>[0]
  /** Merged onto the SURFACE div via `cn()` after `composerSurface(surface)`
   *  (later classes win) — inner padding plus the seam for surface overrides a
   *  sibling adopter needs, e.g. a translucency tweak over `--composer-bg`. */
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
  /** Merged onto the OUTER wrapper (the `relative w-full` positioning context
   *  for the popover) via `cn()` — outer-layout passthrough. Surface-level
   *  overrides go through `bodyClassName`. */
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
