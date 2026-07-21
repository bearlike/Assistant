/**
 * Shared rendering vocabulary for the Identity & Access cards.
 *
 * Every card faces the same four non-data states before it can show a table, so
 * they resolve in ONE place rather than as five copies of the same branch
 * ladder. The distinction that matters: an unavailable surface and a forbidden
 * one are settled ANSWERS, so they read as plain muted sentences; only a
 * genuine transport failure earns the red `ErrorAlert`. Painting "identity
 * management is off" in destructive red would tell an operator something is
 * broken when nothing is.
 */
import type { ReactNode } from "react";
import { Loader2 } from "lucide-react";

import { ErrorAlert } from "@/components/ErrorAlert";
import { cn } from "@/lib/utils";
import type { IamQuery } from "@/hooks/useIam";

/** A muted, non-alarming line — the house style for "nothing to show". */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
      <p className="text-sm text-[hsl(var(--muted-foreground))]">{children}</p>
    </div>
  );
}

export function Spinner() {
  return (
    <div className="flex items-center justify-center py-8">
      <Loader2 className="h-5 w-5 animate-spin text-[hsl(var(--muted-foreground))]" />
    </div>
  );
}

export interface IamCardStateProps<T> {
  query: IamQuery<T>;
  /** Plural noun for the copy, e.g. "people". */
  subject: string;
  /** Shown when the call succeeded but there is nothing in it. */
  emptyLabel: string;
  isEmpty?: (data: T) => boolean;
  children: (data: T) => ReactNode;
}

export function IamCardState<T>({
  query,
  subject,
  emptyLabel,
  isEmpty,
  children,
}: IamCardStateProps<T>) {
  if (query.loading) return <Spinner />;
  if (query.forbidden) {
    return <Notice>Your role does not include permission to view {subject}.</Notice>;
  }
  if (query.unavailable) {
    return (
      <Notice>
        Identity management is switched off for this deployment, so there are no {subject} to
        show. Turn on an authenticator to start managing access.
      </Notice>
    );
  }
  if (query.error) {
    return <ErrorAlert error={query.error} fallback={`Failed to load ${subject}`} />;
  }
  if (!query.data) return <Spinner />;
  if (isEmpty?.(query.data)) return <Notice>{emptyLabel}</Notice>;
  return <>{children(query.data)}</>;
}

/**
 * Says so when a list is showing less than all of it.
 *
 * Every IAM list route pages at 50 by default (clamped to 200), so a table
 * silently stops at 50 rows on any deployment big enough to matter. A list
 * that looks complete and is not is the one failure this whole surface exists
 * to avoid — an operator counting people would simply get the wrong answer.
 * Renders nothing when the page IS everything.
 */
export function TruncationHint({
  page,
  noun,
  hint,
}: {
  page: { items: unknown[]; total: number } | null;
  /** Plural noun for the copy, e.g. "people". */
  noun: string;
  /** How to see the rest, when there is something useful to suggest. */
  hint?: string;
}) {
  if (!page || page.total <= page.items.length) return null;
  return (
    <p className="mt-3 text-xs text-[hsl(var(--muted-foreground))]">
      Showing {page.items.length} of {page.total} {noun}.{hint ? ` ${hint}` : ""}
    </p>
  );
}

/** A small state pill, per the shape law (`rounded-full` = state container). */
export function Pill({
  children,
  tone = "muted",
  className,
}: {
  children: ReactNode;
  tone?: "muted" | "danger" | "accent";
  className?: string;
}) {
  const toneCls =
    tone === "danger"
      ? "bg-[hsl(var(--destructive))]/15 text-[hsl(var(--destructive-text))] border-[hsl(var(--destructive))]/20"
      : tone === "accent"
        ? "bg-[hsl(var(--primary))]/10 text-[hsl(var(--primary-text))] border-[hsl(var(--primary))]/20"
        : "bg-[hsl(var(--muted))]/50 text-[hsl(var(--muted-foreground))] border-[hsl(var(--border))]";
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-1.5 py-0.5 text-2xs font-medium leading-none",
        toneCls,
        className,
      )}
    >
      {children}
    </span>
  );
}

export function PillRow({
  values,
  empty,
  tone,
}: {
  values: readonly string[];
  empty: string;
  tone?: "muted" | "danger" | "accent";
}) {
  if (values.length === 0) {
    return <span className="text-xs text-[hsl(var(--muted-foreground))]">{empty}</span>;
  }
  return (
    <div className="flex flex-wrap gap-1">
      {values.map((value) => (
        <Pill key={value} tone={tone}>
          {value}
        </Pill>
      ))}
    </div>
  );
}
