/**
 * WikiToc — the "On this page" scroll-spy table of contents, extracted from
 * `WikiScreen` so the desktop right rail and the mobile drawer render the
 * IDENTICAL component. Pure presentational: entries and the active heading
 * come in as props, jumps go out through `onNavigate` (the scroll-spy itself
 * stays in `WikiScreen`, which owns the scroller).
 *
 * Renders through the shared rail vocabulary (`nav-rail/rows.tsx`) rather
 * than its own heading/row treatment. This component itself never moves onto
 * the rail — intra-document navigation belongs beside the document it
 * navigates, not in site-wide nav — so its adoption of the shared kit is
 * permanent, not a bridge to a future removal. `depth` carries nesting via
 * indentation and weight only; it no longer drops to a smaller size at
 * level 3.
 */

import { ListTree } from "lucide-react";

import { RailRow, RailSection } from "@/components/nav-rail/rows";

import type { TocEntry } from "./api/types";

interface WikiTocProps {
  items: TocEntry[];
  activeId: string;
  onNavigate: (headingId: string) => void;
  className?: string;
}

export function WikiToc({ items, activeId, onNavigate, className }: WikiTocProps) {
  return (
    <nav className={className} aria-label="On this page">
      {/* Default divider: this section follows `RefreshThisWiki` in both
          mounts, so the rule reads as the boundary between them. */}
      <RailSection label="On this page" icon={ListTree}>
        {items.map((t) => (
          <RailRow
            key={t.id}
            label={t.label}
            title={t.label}
            depth={t.lvl}
            active={activeId === t.id}
            onClick={() => onNavigate(t.id)}
          />
        ))}
      </RailSection>
    </nav>
  );
}
