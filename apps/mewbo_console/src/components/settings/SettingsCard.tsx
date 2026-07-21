/**
 * SettingsCard — the ONE card primitive of the Settings surface.
 *
 * Every card on the page renders through this component: the schema-driven
 * `SettingsSection` AND every custom pane in the facet registry (`panes.ts`).
 * It owns the card class (`sectionCardCls`), the title style
 * (`sectionTitleCls`) and the description renderer (`FieldHelp` — the surface's
 * ONLY help style, first-line + `?` popover for long text). Before this
 * existed, three call sites copy-pasted the same class string and the
 * "chrome-agnostic pane" contract was upheld by convention alone.
 *
 * Keep it a dumb shell: it renders a labelled `<section>` (so it exposes an
 * accessible `region`), an optional header action slot, the body, and an
 * optional footer above a hairline. Any behaviour — Save/Reset, dirty state,
 * data fetching — belongs to the caller.
 */
import { useId, type ReactNode } from "react";

import { FieldHelp } from "./fields/FieldHelp";
import { sectionCardCls, sectionTitleCls } from "./styles";

export interface SettingsCardProps {
  /** Card heading. Also the card's accessible name (via `aria-labelledby`). */
  title: string;
  /** Help text (markdown ok). Rendered through `FieldHelp` — no other style. */
  description?: string;
  /** Header-right slot, e.g. an "Add" button. */
  actions?: ReactNode;
  /** Footer slot below a hairline, e.g. `SettingsSection`'s Save/Reset. */
  footer?: ReactNode;
  /** Heading element id — defaults to a generated one. */
  id?: string;
  children: ReactNode;
}

export function SettingsCard({
  title,
  description,
  actions,
  footer,
  id,
  children,
}: SettingsCardProps) {
  const generatedId = useId();
  const headingId = id ?? generatedId;

  return (
    <section aria-labelledby={headingId} className={sectionCardCls}>
      <div className="mb-4 flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h2 id={headingId} className={sectionTitleCls}>
            {title}
          </h2>
          {description && (
            <div className="mt-1">
              <FieldHelp text={description} />
            </div>
          )}
        </div>
        {actions && <div className="shrink-0">{actions}</div>}
      </div>

      {children}

      {footer && (
        <div className="mt-4 flex items-center gap-2 border-t border-[hsl(var(--border))] pt-4">
          {footer}
        </div>
      )}
    </section>
  );
}
