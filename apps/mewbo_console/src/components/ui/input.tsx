import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * `text-field md:text-sm` is the iOS zoom floor, and it is NOT interchangeable
 * with `text-base md:text-sm`. Mobile Safari zooms the viewport whenever a
 * focused field computes below 16px, and `text-base` is 14px on our scale — so
 * the narrow-viewport size must come from `text-field` (a fixed 16px that does
 * not move with the scale). Desktop has no such hazard, hence the `md:`
 * step-down to 13px for density.
 *
 * ⚠️ The floor step is named `field`, NOT `input`, and renaming it back would
 * break every input in the console. `colors.input` is shadcn's border token, so
 * a `fontSize.input` key emits `.text-input` TWICE — and the colour rule wins on
 * source order, leaving typed text painted in the dim border colour. `cn()`
 * cannot rescue it either: tailwind-merge classifies `text-input` as a colour,
 * so it would drop a foreground class instead of a size.
 *
 * **The primitive owns the type size; callers pass layout, not type.** `cn()`
 * resolves sizes last-wins, so ANY `text-*` size from a call site silently
 * replaces the floor and re-opens the zoom on exactly the narrow viewports it
 * protects. There is no "safe" size to pass — a call site handing a type size
 * to this component is the bug, and the fix belongs there, not here. Pass
 * width, padding, alignment and colour freely.
 *
 * (`text-field` only behaves this way because `lib/utils.ts` registers it in
 * tailwind-merge's `font-size` group. Unregistered, it reads as a COLOUR:
 * it would drop a caller's foreground class and let both sizes reach the DOM,
 * where alphabetical rule order — not intent — picks the winner.)
 */
const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-field shadow-sm transition-colors file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
          className
        )}
        ref={ref}
        {...props}
      />
    )
  }
)
Input.displayName = "Input"

export { Input }
