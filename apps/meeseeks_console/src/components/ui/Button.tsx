import { forwardRef, ButtonHTMLAttributes, AnchorHTMLAttributes, ReactNode } from 'react';
import { cn } from '../../utils/cn';

/**
 * Single button primitive for the console. Commit to pill-shaped
 * (`rounded-full`) buttons across the app — the default for every
 * actionable element. Menu rows and tabs stay linear (not this primitive).
 *
 * Philosophy: pill as default, semantic colors as hover-only affordances,
 * three strict sizes, three variants. See plans/button-design-system.md.
 *
 * Do not add loading state, Slot/asChild, or `as` polymorphism here — the
 * `<AnchorButton>` export covers the `<a>` case, and every other knob is
 * YAGNI. Callers that need one-off layout tweaks append via `className`.
 */
export type ButtonVariant = 'primary' | 'neutral' | 'ghost';
export type ButtonTone = 'default' | 'info' | 'warn' | 'danger';
export type ButtonSize = 'sm' | 'md' | 'lg';

interface CommonProps {
  variant?: ButtonVariant;
  tone?: ButtonTone;
  size?: ButtonSize;
  iconOnly?: boolean;
  leadingIcon?: ReactNode;
  trailingIcon?: ReactNode;
}

// Base classes applied to every button — shape, transitions, focus ring.
const BASE =
  'inline-flex items-center justify-center gap-1.5 rounded-full font-medium ' +
  'transition-colors disabled:opacity-50 disabled:cursor-not-allowed ' +
  'disabled:pointer-events-none ' +
  'focus-visible:outline-none focus-visible:ring-2 ' +
  'focus-visible:ring-[hsl(var(--primary))]/40';

const VARIANT: Record<ButtonVariant, string> = {
  primary:
    'bg-[hsl(var(--primary))] text-[hsl(var(--primary-foreground))] ' +
    'hover:bg-[hsl(var(--primary))]/90 border border-transparent',
  neutral:
    'bg-[hsl(var(--muted))]/60 text-[hsl(var(--foreground))] ' +
    'border border-[hsl(var(--border))] hover:bg-[hsl(var(--muted))]',
  ghost:
    'bg-transparent text-[hsl(var(--muted-foreground))] border border-transparent ' +
    'hover:text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))]',
};

// Hover-only semantic accents. Layered on top of the variant so the resting
// state stays calm — per the article's "reduce distractions" principle.
const TONE: Record<ButtonTone, string> = {
  default: '',
  info: 'hover:text-blue-500 hover:bg-blue-500/10 hover:border-blue-500/30',
  warn: 'hover:text-amber-500 hover:bg-amber-500/10 hover:border-amber-500/30',
  danger: 'hover:text-red-500 hover:bg-red-500/10 hover:border-red-500/30',
};

const SIZE: Record<ButtonSize, string> = {
  sm: 'h-7 px-3 text-xs',
  md: 'h-9 px-4 text-sm',
  lg: 'h-11 px-5 text-base',
};

const SIZE_ICON_ONLY: Record<ButtonSize, string> = {
  sm: 'h-7 w-7',
  md: 'h-9 w-9',
  lg: 'h-11 w-11',
};

function compose(
  variant: ButtonVariant,
  tone: ButtonTone,
  size: ButtonSize,
  iconOnly: boolean,
  extra?: string,
): string {
  return cn(
    BASE,
    VARIANT[variant],
    TONE[tone],
    iconOnly ? SIZE_ICON_ONLY[size] : SIZE[size],
    extra,
  );
}

export interface ButtonProps
  extends CommonProps,
    ButtonHTMLAttributes<HTMLButtonElement> {}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  {
    variant = 'neutral',
    tone = 'default',
    size = 'sm',
    iconOnly = false,
    leadingIcon,
    trailingIcon,
    className,
    children,
    type = 'button',
    ...rest
  },
  ref,
) {
  return (
    <button
      ref={ref}
      type={type}
      className={compose(variant, tone, size, iconOnly, className)}
      {...rest}
    >
      {leadingIcon}
      {children}
      {trailingIcon}
    </button>
  );
});

export interface AnchorButtonProps
  extends CommonProps,
    AnchorHTMLAttributes<HTMLAnchorElement> {}

export const AnchorButton = forwardRef<HTMLAnchorElement, AnchorButtonProps>(
  function AnchorButton(
    {
      variant = 'neutral',
      tone = 'default',
      size = 'sm',
      iconOnly = false,
      leadingIcon,
      trailingIcon,
      className,
      children,
      ...rest
    },
    ref,
  ) {
    return (
      <a
        ref={ref}
        className={compose(variant, tone, size, iconOnly, className)}
        {...rest}
      >
        {leadingIcon}
        {children}
        {trailingIcon}
      </a>
    );
  },
);
