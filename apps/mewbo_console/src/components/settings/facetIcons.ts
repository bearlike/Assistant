/**
 * facetIcons — the ONE `iconName` → lucide component map for Settings facets.
 *
 * `facets.ts` is deliberately React-free, so it names each facet's glyph by
 * lucide *string* and something else has to resolve it. Two surfaces need that
 * resolution — the Settings shell and the NavRail's Settings zone — and each
 * previously kept its own byte-identical copy, with both files carrying a
 * comment admitting the duplication was the thing to watch. It was: the FACET
 * LIST is read from the model, so a new facet always appears in both surfaces,
 * but its GLYPH could silently lag in whichever copy nobody remembered to edit,
 * degrading to the fallback with no error.
 *
 * This module is React-free on purpose (it exports data and a lookup, never a
 * component), which is also what keeps it out of the way of react-refresh: a
 * constant exported from a component module forces a full HMR reload of that
 * module.
 */
import {
  AlarmClock,
  AppWindow,
  Cpu,
  FolderGit2,
  Monitor,
  Plug,
  Puzzle,
  Server,
  Settings2,
  Shield,
  Users,
  Wrench,
  type LucideIcon,
} from "lucide-react";

export const FACET_ICONS: Record<string, LucideIcon> = {
  Cpu,
  Wrench,
  Puzzle,
  AlarmClock,
  AppWindow,
  Plug,
  Monitor,
  Server,
  Shield,
  Users,
  FolderGit2,
  Settings2,
};

/**
 * Resolve a facet's `iconName` to its component.
 *
 * An unmapped name degrades to `Settings2` rather than throwing or rendering
 * nothing — a facet that gains a glyph we haven't mapped yet should still be
 * navigable. The fallback lives here, not at each call site, so the two
 * surfaces cannot disagree about what an unknown name looks like.
 */
export function facetIcon(iconName: string | undefined): LucideIcon {
  return (iconName && FACET_ICONS[iconName]) || Settings2;
}
