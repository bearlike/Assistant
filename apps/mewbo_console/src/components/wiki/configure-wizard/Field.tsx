/**
 * Wizard input shell. Header row (label · optional hint), the slot for the
 * control, and an inline error block.
 */

import { AlertCircle } from "lucide-react";

interface FieldProps {
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: React.ReactNode;
}

export function Field({ label, hint, required, error, children }: FieldProps) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-baseline gap-2">
        <label className="text-xs font-medium text-[hsl(var(--foreground))]">
          {label}
          {required && (
            <span aria-hidden className="text-[hsl(var(--primary))] ml-0.5">
              *
            </span>
          )}
        </label>
        {hint && (
          <span className="text-[11px] text-[hsl(var(--muted-foreground))]">{hint}</span>
        )}
      </div>
      {children}
      {error && (
        <div className="inline-flex items-center gap-1.5 text-[11px] text-red-500 mt-1">
          <AlertCircle className="h-3 w-3" />
          {error}
        </div>
      )}
    </div>
  );
}
