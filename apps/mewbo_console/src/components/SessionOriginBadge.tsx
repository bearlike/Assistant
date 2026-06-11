import { Badge } from './agents';
import { SessionOrigin, SessionSummary } from '../types';

// Origin → badge label + BADGE_COLOR_MAP key. "Manual" stays muted so the
// common case is quiet; internal origins carry a colored chip that names how
// the session was spawned (the landing-page provenance indicator).
const ORIGIN_META: Record<SessionOrigin, { label: string; color: string }> = {
  user: { label: 'Manual', color: 'muted' },
  wiki: { label: 'Wiki', color: 'blue' },
  search: { label: 'Search', color: 'cyan' },
  channel: { label: 'Channel', color: 'emerald' },
  structured: { label: 'Structured', color: 'violet' },
  draft: { label: 'Draft', color: 'teal' },
};

// Channel sessions name their platform instead of the generic "Channel".
const PLATFORM_LABELS: Record<string, string> = {
  'nextcloud-talk': 'Nextcloud',
  email: 'Email',
};

/** Provenance chip shown beside a session's timestamp. */
export function SessionOriginBadge({ session }: { session: SessionSummary }) {
  const origin: SessionOrigin = session.origin ?? 'user';
  const meta = ORIGIN_META[origin] ?? ORIGIN_META.user;
  const label =
    origin === 'channel'
      ? PLATFORM_LABELS[session.context?.source_platform ?? ''] ?? meta.label
      : meta.label;
  return <Badge color={meta.color}>{label}</Badge>;
}
