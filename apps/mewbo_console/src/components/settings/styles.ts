import { cn } from "@/lib/utils";

/**
 * Shared style constants for the faceted Settings UI.
 *
 * `inputBase` is the themed control class string shared by every bare
 * `<input>`/`<select>` in this surface (RJSF's `SelectWidget`, the hook-type
 * and branch `<select>`s). It carries `text-sm` (13px) — fine for a `<select>`,
 * which never triggers the iOS zoom, but under the 16px floor for a real text
 * `<input>`/`<textarea>`. Real text controls use `inputTextCls` below instead;
 * do not point a new `<input>`/`<textarea>` at `inputBase` directly.
 */
export const inputBase =
  "w-full rounded-md border border-[hsl(var(--border-strong))] bg-[hsl(var(--muted))] " +
  "px-3 py-1.5 text-sm text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] " +
  "focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]";

/**
 * `inputBase` with the iOS 16px zoom floor layered on top (`cn()`'s last-wins
 * drops `inputBase`'s own `text-sm`). This is the class every REAL text
 * `<input>`/`<textarea>` in Settings must use — `ScalarInput`, `SecretField`,
 * `KeyedCollectionField`'s key/value inputs, `JsonValueEditor`, and
 * `RecordListField`'s matcher textarea all render through it, which is what
 * makes RJSF's `BaseInputTemplate` (a thin wrapper over `ScalarInput`) safe by
 * inheritance rather than needing its own fix.
 */
export const inputTextCls = cn(inputBase, "text-field md:text-sm");

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

/**
 * The Settings card surface — the ONE card class string for the whole surface.
 * Only `SettingsCard` should read it; every card (schema-driven section AND
 * custom pane) renders through that component, so the "chrome-agnostic pane"
 * contract is enforced by the type system instead of by copy-paste.
 */
export const sectionCardCls =
  "rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6";

/** Section card title (the section <summary>/h2 level). A headline, so it
 *  gets the surface's one semibold step; everything else on this page is
 *  either body/metadata (normal) or emphasis (medium) — see the weight ramp
 *  in the console CLAUDE.md's "Settings" section. */
export const sectionTitleCls = "text-sm font-semibold text-[hsl(var(--foreground))]";

/** Nested object (subsection) title. */
export const subsectionTitleCls = "text-xs font-medium text-[hsl(var(--foreground))]";
