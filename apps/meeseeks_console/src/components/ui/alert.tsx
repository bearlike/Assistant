import * as React from "react";
import { cn } from "../../utils/cn";
type AlertProps = React.HTMLAttributes<HTMLDivElement> & {
  variant?: "default" | "destructive";
};
export const Alert = React.forwardRef<HTMLDivElement, AlertProps>(({
  className,
  variant = "default",
  ...props
}, ref) => <div ref={ref} role="alert" className={cn("relative w-full rounded-lg border px-3 py-2 text-sm", variant === "destructive" ? "border-red-900/60 bg-red-950/50 text-red-200" : "border-zinc-800 bg-zinc-900/40 text-zinc-200", className)} {...props} />);
Alert.displayName = "Alert";
export const AlertTitle = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLHeadingElement>>(({
  className,
  ...props
}, ref) => <h5 ref={ref} className={cn("mb-1 text-xs font-semibold uppercase", className)} {...props} />);
AlertTitle.displayName = "AlertTitle";
export const AlertDescription = React.forwardRef<HTMLParagraphElement, React.HTMLAttributes<HTMLParagraphElement>>(({
  className,
  ...props
}, ref) => <div ref={ref} className={cn("text-sm", className)} {...props} />);
AlertDescription.displayName = "AlertDescription";