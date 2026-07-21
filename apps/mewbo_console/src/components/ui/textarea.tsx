import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * `text-field md:text-sm` is the iOS zoom floor — see the note on `Input`, which
 * also records why the step is named `field` and must never be renamed to
 * `input`. `text-base` is 14px on our scale and would make mobile Safari zoom
 * the viewport on focus; `text-field` is a fixed 16px that does not move with
 * the scale.
 */
const Textarea = React.forwardRef<
  HTMLTextAreaElement,
  React.ComponentProps<"textarea">
>(({ className, ...props }, ref) => {
  return (
    <textarea
      className={cn(
        "flex min-h-[60px] w-full rounded-md border border-input bg-transparent px-3 py-2 text-field shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 md:text-sm",
        className
      )}
      ref={ref}
      {...props}
    />
  )
})
Textarea.displayName = "Textarea"

export { Textarea }
