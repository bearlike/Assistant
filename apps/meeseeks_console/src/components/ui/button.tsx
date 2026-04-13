import * as React from "react"
import { Slot } from "@radix-ui/react-slot"
import { cva, type VariantProps } from "class-variance-authority"

import { cn } from "@/lib/utils"

/**
 * Console Button — shadcn cva structure with our pill-shaped variant matrix.
 *
 * variants: primary | neutral | ghost
 * sizes:    sm | md | lg
 * tones:    default | info | warn | danger (hover-only semantic accents)
 * iconOnly: switches size to a square preset
 * leadingIcon / trailingIcon: optional adornments rendered around children
 *
 * Pill-shaped (`rounded-full`) is the default. Tone is layered on top of the
 * variant so resting state stays calm; semantic colors only appear on hover.
 */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 rounded-full font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed disabled:pointer-events-none focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--primary))]/40",
  {
    variants: {
      variant: {
        primary:
          "bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] hover:bg-[hsl(var(--primary))]/90 border border-transparent",
        neutral:
          "bg-[hsl(var(--muted))]/60 text-[hsl(var(--foreground))] border border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]",
        ghost:
          "bg-transparent text-[hsl(var(--muted-foreground))] border border-transparent hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]",
      },
      tone: {
        default: "",
        info: "hover:text-blue-500 hover:bg-blue-500/10 hover:border-blue-500/30",
        warn: "hover:text-amber-500 hover:bg-amber-500/10 hover:border-amber-500/30",
        danger: "hover:text-red-500 hover:bg-red-500/10 hover:border-red-500/30",
      },
      size: {
        sm: "h-7 px-3 text-xs",
        md: "h-9 px-4 text-sm",
        lg: "h-11 px-5 text-base",
      },
      iconOnly: {
        true: "",
        false: "",
      },
    },
    compoundVariants: [
      { iconOnly: true, size: "sm", class: "h-7 w-7 px-0" },
      { iconOnly: true, size: "md", class: "h-9 w-9 px-0" },
      { iconOnly: true, size: "lg", class: "h-11 w-11 px-0" },
    ],
    defaultVariants: {
      variant: "neutral",
      tone: "default",
      size: "sm",
      iconOnly: false,
    },
  }
)

export type ButtonVariant = "primary" | "neutral" | "ghost"
export type ButtonTone = "default" | "info" | "warn" | "danger"
export type ButtonSize = "sm" | "md" | "lg"

export interface ButtonProps
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, "type">,
    Omit<VariantProps<typeof buttonVariants>, "iconOnly"> {
  asChild?: boolean
  iconOnly?: boolean
  leadingIcon?: React.ReactNode
  trailingIcon?: React.ReactNode
  type?: "button" | "submit" | "reset"
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  (
    {
      className,
      variant,
      tone,
      size,
      iconOnly = false,
      asChild = false,
      leadingIcon,
      trailingIcon,
      type = "button",
      children,
      ...props
    },
    ref
  ) => {
    const Comp = asChild ? Slot : "button"
    const composed = cn(buttonVariants({ variant, tone, size, iconOnly, className }))
    if (asChild) {
      // Slot expects a single child; ignore leading/trailing icons in this mode.
      return <Comp className={composed} ref={ref} {...props}>{children}</Comp>
    }
    return (
      <Comp className={composed} ref={ref} type={type} {...props}>
        {leadingIcon}
        {children}
        {trailingIcon}
      </Comp>
    )
  }
)
Button.displayName = "Button"

// eslint-disable-next-line react-refresh/only-export-components
export { Button, buttonVariants }
