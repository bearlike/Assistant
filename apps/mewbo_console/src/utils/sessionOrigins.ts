import {
  AppWindow,
  BookOpen,
  Braces,
  ListChecks,
  MessagesSquare,
  PenLine,
  Search,
  Smartphone,
  type LucideIcon,
} from 'lucide-react';

import { SessionOrigin } from '../types';

/** Everything a surface needs to render one origin. */
export interface OriginMeta {
  /** Chip text on a session row — names THIS session's provenance. */
  label: string;
  /** Row text in the origin filter menu, when it differs from the chip: a
   *  filter row names a SET of sessions, a chip names one. Omitted when the
   *  same word reads correctly in both places. */
  filterLabel?: string;
  /** `BADGE_COLOR_MAP` key (`utils/agents.ts`) — add the key there first. */
  color: string;
  /** Leading glyph, shared by the chip and the filter row so a user learns one
   *  mark per class. The four that name a product deliberately reuse that
   *  product's NavRail glyph (`nav-rail/products.ts`): a wiki session and the
   *  Wiki rail row must not teach two different marks for the same thing. */
  icon: LucideIcon;
}

/**
 * The ONE per-origin registry. Label, colour and glyph live together because
 * every surface that names an origin needs all three, and two of them were
 * previously spelled twice (the filter menu owned labels, the badge owned
 * labels + colours) — which is how "My tasks" and "Manual" came to describe the
 * same origin with no shared home.
 *
 * Exhaustive by construction: a new member of the core `SessionOrigin` union
 * that is missing here is a TS error, which is the point.
 */
export const ORIGIN_META: Record<SessionOrigin, OriginMeta> = {
  // "Manual" stays muted so the common case is quiet; every internal origin
  // carries a colored chip that names how the session was spawned.
  user: { label: 'Manual', filterLabel: 'My tasks', color: 'muted', icon: ListChecks },
  channel: { label: 'Channel', filterLabel: 'Channels', color: 'emerald', icon: MessagesSquare },
  wiki: { label: 'Wiki', color: 'blue', icon: BookOpen },
  search: { label: 'Search', color: 'cyan', icon: Search },
  apps: { label: 'App', filterLabel: 'Apps', color: 'fuchsia', icon: AppWindow },
  structured: { label: 'Structured', color: 'violet', icon: Braces },
  draft: { label: 'Draft', color: 'teal', icon: PenLine },
  mobile: { label: 'Mobile', color: 'amber', icon: Smartphone },
};

/**
 * Filter-menu order: what the user authored first, then the four products the
 * NavRail switches between, then the origins minted by direct API callers.
 * Explicit rather than `Object.keys(ORIGIN_META)` — key order is a language
 * detail, and this is a designed reading order.
 */
const ORIGIN_ORDER: readonly SessionOrigin[] = [
  'user',
  'channel',
  'wiki',
  'search',
  'apps',
  'structured',
  'draft',
  'mobile',
];

/**
 * Per-origin filter shared by the landing page (`HomeView`) and the NavRail's
 * Tasks section. Default reveals what the user authored — sessions they started
 * in the console ("user") plus channel chats — and hides the internally-spawned
 * wiki / search / apps / structured / draft / mobile sessions until scoped into.
 * Keep this the single source of truth so both surfaces show the same set.
 */
export const ORIGIN_FILTERS: { origin: SessionOrigin; label: string; icon: LucideIcon }[] =
  ORIGIN_ORDER.map((origin) => ({
    origin,
    label: ORIGIN_META[origin].filterLabel ?? ORIGIN_META[origin].label,
    icon: ORIGIN_META[origin].icon,
  }));

export const DEFAULT_VISIBLE_ORIGINS: SessionOrigin[] = ['user', 'channel'];

/** Whether an origin is shown by default. Missing origin falls back to 'user'
 *  (matches core's default provenance), so legacy rows stay visible. */
export function isDefaultVisibleOrigin(origin?: SessionOrigin): boolean {
  return DEFAULT_VISIBLE_ORIGINS.includes(origin ?? 'user');
}
