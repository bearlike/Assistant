import { SessionOrigin } from '../types';

// Per-origin filter shared by the landing page (HomeView) and the persistent
// task sidebar (TaskSidebar). Default reveals what the user authored —
// sessions they started in the console ("user") plus channel chats — and hides
// the internally-spawned wiki/search/structured/draft sessions until scoped
// into. Keep this the single source of truth so both surfaces show the same
// user-facing set.
export const ORIGIN_FILTERS: { origin: SessionOrigin; label: string }[] = [
  { origin: 'user', label: 'My tasks' },
  { origin: 'channel', label: 'Channels' },
  { origin: 'wiki', label: 'Wiki' },
  { origin: 'search', label: 'Search' },
  { origin: 'structured', label: 'Structured' },
  { origin: 'draft', label: 'Draft' },
];

export const DEFAULT_VISIBLE_ORIGINS: SessionOrigin[] = ['user', 'channel'];

/** Whether an origin is shown by default. Missing origin falls back to 'user'
 *  (matches core's default provenance), so legacy rows stay visible. */
export function isDefaultVisibleOrigin(origin?: SessionOrigin): boolean {
  return DEFAULT_VISIBLE_ORIGINS.includes(origin ?? 'user');
}
