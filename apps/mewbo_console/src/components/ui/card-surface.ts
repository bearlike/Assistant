import { cva } from "class-variance-authority"

/**
 * `cardSurface()` — the ONE builder for a console card's chrome: the hairline
 * border + `--card` fill that ~60 sites hand-copied as the literal
 * `border border-[hsl(var(--border))] bg-[hsl(var(--card))]`, plus the radius
 * (keyed to the shape vocabulary) and optional elevation.
 *
 * It owns CHROME ONLY — border, background, radius, box-shadow. Padding,
 * spacing, layout and any positional overrides stay at the CALL SITE via
 * `cn(cardSurface({ ... }), "p-4 space-y-3")` — the same split as
 * `composerSurface()` in `ui/composer-shell.tsx`, which owns the composer
 * surface but not its padding. This is a class-string builder, not a component.
 *
 * ## Shape-vocabulary contract (Design Philosophy → "Shape vocabulary")
 * `radius` names the card's ROLE, not a pixel value; each maps to the documented
 * radius for that surface (`--radius` is 0.5rem, so `rounded-lg` = 8px,
 * `rounded-md` = 6px):
 *
 *   - `right` → `rounded-md`  (6px)  — right / instrument-panel cards, log &
 *              tool cards, compact rows, micro-surfaces.
 *   - `left`  → `rounded-lg`  (8px)  — left / reading-surface cards and Settings
 *              section cards (the dominant family).
 *   - `panel` → `rounded-xl`  (12px) — larger inline feature panels
 *              (answer / app / welcome cards).
 *   - `modal` → `rounded-2xl` (16px) — hero / overlay panels (configure wizard,
 *              indexing screen, empty-state heroes).
 *
 * `rounded-full` is deliberately ABSENT: it is reserved for state containers
 * (badges, pills, scrollbars, the brand mark), which are NOT card surfaces —
 * reach for `Badge` / the status primitives, never this.
 *
 * Adoption is BEHAVIOR-PRESERVING: pick the variant matching a card's CURRENT
 * radius; do not restyle a card while moving it onto the builder.
 *
 * ## Elevation
 * Optional; defaults to flat (`none`). It emits the `[box-shadow:var(--elev-N)]`
 * arbitrary-property form on purpose — a bare `shadow-[var(--elev-N)]` silently
 * renders NO shadow under Tailwind v3 (the `shadow-` prefix is ambiguous; see
 * the console CLAUDE.md "elev-shadow trap"). Adopting this variant is also how a
 * hand-rolled `shadow-[0_..._rgba(...)]` panel folds onto the `--elev-*` tokens.
 */
const cardSurfaceVariants = cva(
  "border border-[hsl(var(--border))] bg-[hsl(var(--card))]",
  {
    variants: {
      radius: {
        right: "rounded-md",
        left: "rounded-lg",
        panel: "rounded-xl",
        modal: "rounded-2xl",
      },
      elevation: {
        none: "",
        "elev-1": "[box-shadow:var(--elev-1)]",
        "elev-2": "[box-shadow:var(--elev-2)]",
        "elev-3": "[box-shadow:var(--elev-3)]",
      },
    },
    defaultVariants: {
      elevation: "none",
    },
  },
)

export function cardSurface(opts: {
  /** Shape-vocabulary family (see the contract above). Required, so every card
   *  states which surface it belongs to — there is no silent default radius. */
  radius: "right" | "left" | "panel" | "modal"
  /** Elevation token; omit for a flat card. Emits `[box-shadow:var(--elev-N)]`
   *  (the working form — `shadow-[var(--elev-N)]` renders nothing). */
  elevation?: "none" | "elev-1" | "elev-2" | "elev-3"
}): string {
  return cardSurfaceVariants(opts)
}
