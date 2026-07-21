import { useState } from 'react';
import {
  CheckCircle2,
  ChevronDown,
  ClipboardCheck,
  ThumbsDown,
  ThumbsUp,
  XCircle,
} from 'lucide-react';
import type { PlanMeta } from '../types';
import { MarkdownContent } from './MessageBubble';

interface PlanCardProps {
  plan: PlanMeta;
  onApprove?: (approved: boolean) => void;
}

/**
 * Inline plan proposal card rendered between a user query and the assistant
 * response in ConversationTimeline. Owns its own expand/collapse state and
 * shows Approve/Reject buttons only while `plan.status === "pending"`.
 *
 * After resolution (approved/rejected) the card stays at the same location
 * in the timeline with a status badge, so the history of revisions is
 * visible without needing to open the trace panel.
 */
export function PlanCard({ plan, onApprove }: PlanCardProps) {
  const [expanded, setExpanded] = useState(false);
  const { status, revision, planContent } = plan;

  const StatusIcon =
    status === 'pending'
      ? ClipboardCheck
      : status === 'approved'
        ? CheckCircle2
        : XCircle;
  const iconClass =
    status === 'pending'
      ? 'text-[hsl(var(--info-text))]'
      : status === 'approved'
        ? 'text-[hsl(var(--success))]'
        : 'text-[hsl(var(--warning-text))]';
  const badgeClass =
    status === 'pending'
      ? 'bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info-text))] border-[hsl(var(--info)/0.3)]'
      : status === 'approved'
        ? 'bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))] border-[hsl(var(--success)/0.3)]'
        : 'bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning-text))] border-[hsl(var(--warning)/0.3)]';
  const statusLabel =
    status === 'pending'
      ? 'Awaiting approval'
      : status === 'approved'
        ? 'Approved'
        : 'Rejected';
  const title = revision > 1 ? `Plan (revision ${revision})` : 'Plan';

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        className="w-full flex items-center gap-3 px-4 py-3 text-left hover:bg-[hsl(var(--accent))]/40 transition-colors"
      >
        <StatusIcon className={`w-4 h-4 shrink-0 ${iconClass}`} />
        <span className="text-sm font-medium text-[hsl(var(--foreground))]">
          {title}
        </span>
        <span
          className={`text-2xs font-medium px-2 py-0.5 rounded-full border ${badgeClass}`}
        >
          {statusLabel}
        </span>
        <ChevronDown
          className={`w-4 h-4 ml-auto text-[hsl(var(--muted-foreground))] transition-transform ${expanded ? 'rotate-180' : ''}`}
        />
      </button>
      {expanded && (
        <div className="border-t border-[hsl(var(--border))] px-4 py-3">
          <div className="text-xs text-[hsl(var(--foreground))] leading-relaxed [&_pre]:text-2xs [&_p]:mb-1 [&_p:last-child]:mb-0 min-w-0 overflow-hidden">
            <MarkdownContent content={planContent} />
          </div>
          {status === 'pending' && onApprove && (
            <div className="flex flex-wrap items-center gap-2 mt-4 pt-3 border-t border-[hsl(var(--border))]">
              <button
                type="button"
                onClick={() => onApprove(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-[hsl(var(--success)/0.4)] bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))] hover:bg-[hsl(var(--success)/0.2)] transition-colors"
              >
                <ThumbsUp className="w-3.5 h-3.5" />
                Approve
              </button>
              <button
                type="button"
                onClick={() => onApprove(false)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-[hsl(var(--warning)/0.4)] bg-[hsl(var(--warning)/0.1)] text-[hsl(var(--warning-text))] hover:bg-[hsl(var(--warning)/0.2)] transition-colors"
              >
                <ThumbsDown className="w-3.5 h-3.5" />
                Reject
              </button>
              <span className="text-2xs text-[hsl(var(--muted-foreground))] ml-1">
                Reject and type refinement guidance in the chat input.
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
