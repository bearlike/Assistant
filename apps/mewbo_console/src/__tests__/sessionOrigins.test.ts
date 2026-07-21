import { describe, expect, it } from 'vitest';

import {
  DEFAULT_VISIBLE_ORIGINS,
  ORIGIN_FILTERS,
  ORIGIN_META,
  isDefaultVisibleOrigin,
} from '../utils/sessionOrigins';
import { BADGE_COLOR_MAP } from '../utils/agents';
import type { SessionOrigin } from '../types';

/**
 * `ORIGIN_META` is exhaustive by TYPE (`Record<SessionOrigin, …>`), so a new
 * core origin cannot be forgotten there. Everything downstream of it has no
 * such compile-time guard, which is what these assertions cover.
 */
describe('session origin registry', () => {
  it('offers every origin in the filter menu, exactly once', () => {
    // `ORIGIN_ORDER` behind `ORIGIN_FILTERS` is a plain array: TS accepts one
    // that omits a member, so an origin could exist as a badge yet be
    // unfilterable — invisible on the landing page with no way to reveal it.
    const filtered = ORIGIN_FILTERS.map((entry) => entry.origin).sort();
    const known = (Object.keys(ORIGIN_META) as SessionOrigin[]).sort();
    expect(filtered).toEqual(known);
  });

  it('binds every origin to a real badge colour and a glyph', () => {
    // The colour is a `BADGE_COLOR_MAP` KEY, not a class string, so a typo or a
    // hue that was never added there degrades silently to the muted fallback.
    for (const [origin, meta] of Object.entries(ORIGIN_META)) {
      expect(BADGE_COLOR_MAP, `${origin} colour`).toHaveProperty(meta.color);
      // A lucide icon is a `forwardRef` object, not a bare function — assert it
      // is renderable, not what shape React happens to wrap it in.
      expect(meta.icon, `${origin} icon`).toBeTruthy();
      expect(meta.label.length, `${origin} label`).toBeGreaterThan(0);
    }
  });

  it('shows only user-authored and channel sessions by default', () => {
    expect(DEFAULT_VISIBLE_ORIGINS).toEqual(['user', 'channel']);
    expect(isDefaultVisibleOrigin('apps')).toBe(false);
    expect(isDefaultVisibleOrigin('wiki')).toBe(false);
    // A row whose origin predates the field falls back to 'user' so it stays
    // visible rather than vanishing from the landing page.
    expect(isDefaultVisibleOrigin(undefined)).toBe(true);
  });
});
