import { MouseEvent, useMemo, useState } from 'react';
import { AlertCircle, Play, RotateCcw } from 'lucide-react';
import { LogEventCard } from './LogEventCard';
import { CopyButton } from './CopyButton';
import { Badge } from './agents';
import { Button } from './ui/button';
import { ModelBrandIcon } from './ModelBrandIcon';
import { ModelPicker } from './wiki/ModelPicker';
import { formatModelName } from '../utils/model';
import { SCROLLBAR_CLASS } from '../utils/scrollbar';
import type { RunErrorKind, RunFailureMeta, RunFailureReason } from '../types';

/** Fallback headline when the backend classified the failure but sent no title. */
const KIND_TITLES: Record<RunErrorKind, string> = {
  upstream_bad_gateway: 'Upstream bad gateway',
  rate_limited: 'Rate limited',
  timeout: 'Timed out',
  auth: 'Authentication failed',
  context_overflow: 'Context window exceeded',
  provider_unavailable: 'Provider unavailable',
  tool_failure: 'Tool failure',
  unknown: '',
};

const REASON_LABELS: Record<RunFailureReason, string> = {
  error: 'Run failed',
  max_steps_reached: 'Task interrupted — step limit reached',
  interrupted: 'Interrupted — never concluded',
  unmet_goal: 'Goal not met',
  blocked: 'Blocked',
};

/** Human phrase for a `blocked_code`, so the card names what to fix rather
 *  than echoing a raw token. An unknown code falls back to itself. */
const BLOCKED_ON: Record<string, string> = {
  repo_access: 'repository access',
  network: 'a network resource',
  forbidden: 'a permission',
  quota_exceeded: 'quota',
};

/**
 * Classifier reasons where the model itself is a plausible cause — degraded
 * availability, a regional outage, exhausted quota. Recovery defaults to the
 * next rung of the ladder for these, because retrying on the model that just
 * failed amplifies the failure.
 *
 * Every OTHER reason (`bad_request`, `content_policy`, `permission_denied`,
 * anything the backend adds later) defaults to the SAME model on purpose: a
 * deterministic failure reproduces everywhere, and switching only hides it.
 */
const MODEL_ATTRIBUTABLE: ReadonlySet<string> = new Set([
  'timeout',
  'rate_limit',
  'server_error',
  'bad_gateway',
  'connection',
  'quota_exhausted',
  'auth',
  'no_deployments',
  'invalid_model',
]);

interface RunFailedCardProps {
  failure: RunFailureMeta;
  timestamp?: string;
  /**
   * Recovery actions, each receiving the model the user chose (undefined =
   * run on the session's persisted model). Present ONLY on the latest failure
   * while the session is idle — earlier failures are history and render
   * read-only. Both must be supplied together or neither renders.
   */
  onRetry?: (model?: string) => void;
  onContinue?: (model?: string) => void;
  /** The model this session runs on — the head of the chain, and the fallback
   *  when no ladder is declared. */
  model?: string | null;
  /** The session's declared fallback ladder, in order. */
  fallbackModels?: string[];
}

/**
 * The single "run failed" readout, shared by the conversation timeline and the
 * trace panel. Composes {@link LogEventCard} so it inherits the log-card shape
 * vocabulary, expand/collapse and accent rail.
 *
 * Two things are load-bearing. Recovery lives in the COLLAPSED header, so a
 * user never has to expand a wall of provider HTML to retry. And the error body
 * is bounded (`max-h-[300px]`) — an upstream 502 page arrives as thousands of
 * characters, which an unbounded `<p>` renders as a page-length wall.
 */
export function RunFailedCard({
  failure,
  timestamp,
  onRetry,
  onContinue,
  model,
  fallbackModels,
}: RunFailedCardProps) {
  const { reason, text, detail, failureReason, modelsTried, blockedCode } = failure;
  // Secondary line beside the headline: the classified provider title when
  // there is one, else — for a blocked run — what the run was blocked on, so
  // the user reads the actionable cause without expanding anything.
  const title = detail
    ? detail.title.trim() || KIND_TITLES[detail.kind]
    : blockedCode
      ? `waiting on ${BLOCKED_ON[blockedCode] ?? blockedCode}`
      : '';
  const canRecover = Boolean(onRetry && onContinue);
  const attributable = MODEL_ATTRIBUTABLE.has(failureReason ?? '');

  /**
   * The model recovery starts on unless the user says otherwise. For a
   * model-attributable failure that is the next untried rung of
   * `[primary, ...ladder]`; for anything else — and whenever no ladder is
   * declared, so there IS no next rung — it stays the model that just ran.
   * A proposal, never a forced switch.
   */
  const suggested = useMemo(() => {
    const tried = modelsTried ?? [];
    const last = tried[tried.length - 1] ?? model ?? '';
    if (!attributable) return last;
    const chain = [model ?? '', ...(fallbackModels ?? [])].filter(Boolean);
    // A `last` absent from the chain leaves `indexOf` at -1, which walks the
    // whole chain from the top — the right degradation, since the already
    // tried filter still keeps it off a model that just failed.
    const after = chain.slice(chain.indexOf(last) + 1);
    return after.find((m) => m !== last && !tried.includes(m)) ?? last;
  }, [attributable, model, fallbackModels, modelsTried]);

  // Null until the user overrides the proposal; "" is a real choice meaning
  // "no override, run on whatever the session persisted".
  const [chosen, setChosen] = useState<string | null>(null);
  const recoveryModel = chosen ?? suggested;

  // LogEventCard's root toggles expansion on click, so every control living in
  // its header has to stop the event from bubbling up to it.
  const act = (fn?: (model?: string) => void) => (e: MouseEvent) => {
    e.stopPropagation();
    fn?.(recoveryModel || undefined);
  };

  return (
    <LogEventCard
      icon={<AlertCircle className="w-4 h-4 text-[hsl(var(--destructive))]" />}
      title={
        <span className="flex min-w-0 items-baseline gap-2">
          <span className="shrink-0">{REASON_LABELS[reason]}</span>
          {title && (
            <span className="truncate text-xs font-normal text-[hsl(var(--muted-foreground))]">
              {title}
            </span>
          )}
        </span>
      }
      badge={
        <span className="flex shrink-0 items-center gap-1.5">
          {detail?.provider && <Badge color="red">{detail.provider}</Badge>}
          {canRecover && (
            <>
              {/* The popover content portals out, but the trigger's own click
                  still bubbles into the card's expand handler. */}
              <span onClick={(e) => e.stopPropagation()}>
                <ModelPicker
                  value={recoveryModel}
                  onChange={setChosen}
                  variant="compact"
                  className="max-w-[150px]"
                  defaultLabel="Session default"
                  title={
                    attributable
                      ? `Recovery model — proposed the next model in the chain because this run failed with "${failureReason}"`
                      : 'Recovery model — kept as-is; this failure is not one a different model would fix'
                  }
                />
              </span>
              <Button
                variant="neutral"
                size="sm"
                tone="info"
                leadingIcon={<RotateCcw className="w-3 h-3" />}
                onClick={act(onRetry)}
                title="Re-run the last user query from where it was submitted"
              >
                Retry
              </Button>
              <Button
                variant="neutral"
                size="sm"
                tone="warn"
                leadingIcon={<Play className="w-3 h-3" />}
                onClick={act(onContinue)}
                title="Resume the session and let the agent recover from where it left off"
              >
                Continue
              </Button>
            </>
          )}
        </span>
      }
      timestamp={timestamp}
      accent="red"
      // Always visible, never behind the expander: it is the evidence the
      // model choice sitting in the header is made against.
      belowHeader={
        modelsTried && modelsTried.length > 0 ? (
          <div className="flex flex-wrap items-center gap-x-1.5 gap-y-1 px-3 pb-2 text-2xs text-[hsl(var(--muted-foreground))]">
            <span>Tried</span>
            {modelsTried.map((tried, i) => (
              <span key={`${tried}-${i}`} className="flex items-center gap-1">
                <ModelBrandIcon modelId={tried} size={10} />
                <span>{formatModelName(tried)}</span>
              </span>
            ))}
          </div>
        ) : undefined
      }
    >
      {text ? (
        <div>
          <div className="relative">
            <pre
              className={`max-h-[300px] overflow-y-scroll rounded-md border border-[hsl(var(--code-border))] bg-[hsl(var(--code-body))] py-2 pl-3 pr-10 font-mono text-xs leading-relaxed text-[hsl(var(--code-stderr))] whitespace-pre-wrap break-all ${SCROLLBAR_CLASS}`}
            >
              {text}
            </pre>
            <CopyButton
              text={text}
              label="Copy error detail"
              className="absolute right-1 top-1 text-[hsl(var(--code-fg-muted))] hover:text-[hsl(var(--code-fg))]"
            />
          </div>
          {detail?.truncated && (
            <p className="mt-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
              {`Showing first ${text.length.toLocaleString()} of ${detail.detail_chars.toLocaleString()} characters`}
            </p>
          )}
        </div>
      ) : null}
    </LogEventCard>
  );
}
