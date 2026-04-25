import * as React from "react"

import { cn } from "@/lib/utils"

/**
 * Minimal switch primitive — intentionally implemented as a styled button so
 * we don't take a new Radix dependency for a single use site. API matches
 * shadcn's `<Switch checked onCheckedChange />` so a swap to
 * `@radix-ui/react-switch` later is a one-import change.
 */
export interface SwitchProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "onChange"> {
  checked?: boolean
  defaultChecked?: boolean
  onCheckedChange?: (checked: boolean) => void
}

export const Switch = React.forwardRef<HTMLButtonElement, SwitchProps>(
  ({ className, checked, defaultChecked, onCheckedChange, disabled, ...props }, ref) => {
    const [internal, setInternal] = React.useState(defaultChecked ?? false)
    const isControlled = checked !== undefined
    const value = isControlled ? checked : internal

    const toggle = () => {
      if (disabled) return
      const next = !value
      if (!isControlled) setInternal(next)
      onCheckedChange?.(next)
    }

    return (
      <button
        ref={ref}
        type="button"
        role="switch"
        aria-checked={value}
        disabled={disabled}
        onClick={toggle}
        className={cn(
          "peer inline-flex h-5 w-9 shrink-0 cursor-pointer items-center rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50",
          value
            ? "bg-[hsl(var(--primary))]"
            : "bg-[hsl(var(--muted))]",
          className
        )}
        {...props}
      >
        <span
          className={cn(
            "pointer-events-none block h-4 w-4 rounded-full bg-white shadow-lg ring-0 transition-transform",
            value ? "translate-x-4" : "translate-x-0"
          )}
        />
      </button>
    )
  }
)
Switch.displayName = "Switch"
