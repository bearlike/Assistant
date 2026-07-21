import { Badge } from './agents';
import { SessionOrigin, SessionSummary } from '../types';
import { ORIGIN_META } from '../utils/sessionOrigins';

// Channel sessions name their platform instead of the generic "Channel".
const PLATFORM_LABELS: Record<string, string> = {
  'nextcloud-talk': 'Nextcloud',
  email: 'Email',
};

/** Provenance chip shown beside a session's timestamp. Label, colour and glyph
 *  all come from the shared `ORIGIN_META` registry, so this chip and the
 *  landing page's origin filter can never name the same class two ways. */
export function SessionOriginBadge({ session }: { session: SessionSummary }) {
  const origin: SessionOrigin = session.origin ?? 'user';
  const meta = ORIGIN_META[origin] ?? ORIGIN_META.user;
  const label =
    origin === 'channel'
      ? PLATFORM_LABELS[session.context?.source_platform ?? ''] ?? meta.label
      : meta.label;
  const Icon = meta.icon;
  return (
    <Badge color={meta.color}>
      {/* The glyph is the fast read at a glance; the word is what makes it
          unambiguous. `size-3` keeps it proportionate to the chip's 11px type
          without becoming a second focal point. */}
      <span className="inline-flex items-center gap-1">
        <Icon className="size-3 shrink-0" aria-hidden />
        {label}
      </span>
    </Badge>
  );
}
