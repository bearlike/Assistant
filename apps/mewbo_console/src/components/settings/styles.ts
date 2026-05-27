/**
 * Shared style constants for the faceted Settings UI.
 *
 * `inputBase` is the themed text-input class string used by both the RJSF
 * `BaseInputTemplate` (RjsfTheme.tsx) and the write-only `SecretField` widget.
 * Kept here so the two stay byte-for-byte identical (DRY).
 */
export const inputBase =
  "w-full rounded-md border border-[hsl(var(--border-strong))] bg-[hsl(var(--input))] " +
  "px-3 py-1.5 text-sm text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] " +
  "focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]";

/**
 * One typography scale for the whole Settings surface. Every label/help/title
 * renderer pulls from these so the page has a single visual rhythm — no more
 * 12px-vs-16px description split. Mirrors the prior section-card h2 and
 * field-label sizes so nothing regresses visually.
 */

/** Leaf field label — block, xs, medium, full foreground. */
export const labelCls = "block text-xs font-medium text-[hsl(var(--foreground))]";

/** Help / description text — xs, muted. The ONLY help styling. */
export const helpCls = "text-xs text-[hsl(var(--muted-foreground))]";

/** Section card title (the section <summary>/h2 level). */
export const sectionTitleCls = "text-sm font-semibold text-[hsl(var(--foreground))]";

/** Nested object (subsection) title. */
export const subsectionTitleCls = "text-xs font-semibold text-[hsl(var(--foreground))]";
