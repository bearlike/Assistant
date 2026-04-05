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
  const [expanded, setExpanded] = useState(plan.status === 'pending');
  const { status, revision, planContent } = plan;

  const StatusIcon =
    status === 'pending'
      ? ClipboardCheck
      : status === 'approved'
        ? CheckCircle2
        : XCircle;
  const iconClass =
    status === 'pending'
      ? 'text-cyan-600'
      : status === 'approved'
        ? 'text-emerald-600'
        : 'text-amber-600';
  const badgeClass =
    status === 'pending'
      ? 'bg-cyan-500/10 text-cyan-700 border-cyan-500/30'
      : status === 'approved'
        ? 'bg-emerald-500/10 text-emerald-700 border-emerald-500/30'
        : 'bg-amber-500/10 text-amber-700 border-amber-500/30';
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
        <span className="text-sm font-semibold text-[hsl(var(--foreground))]">
          {title}
        </span>
        <span
          className={`text-[10px] font-medium px-2 py-0.5 rounded-full border ${badgeClass}`}
        >
          {statusLabel}
        </span>
        <ChevronDown
          className={`w-4 h-4 ml-auto text-[hsl(var(--muted-foreground))] transition-transform ${expanded ? 'rotate-180' : ''}`}
        />
      </button>
      {expanded && (
        <div className="border-t border-[hsl(var(--border))] px-4 py-3">
          <div className="text-xs text-[hsl(var(--foreground))] leading-relaxed [&_pre]:text-[11px] [&_p]:mb-1 [&_p:last-child]:mb-0 min-w-0 overflow-hidden">
            <MarkdownContent content={planContent} />
          </div>
          {status === 'pending' && onApprove && (
            <div className="flex flex-wrap items-center gap-2 mt-4 pt-3 border-t border-[hsl(var(--border))]">
              <button
                type="button"
                onClick={() => onApprove(true)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-emerald-500/40 bg-emerald-500/10 text-emerald-700 hover:bg-emerald-500/20 transition-colors"
              >
                <ThumbsUp className="w-3.5 h-3.5" />
                Approve
              </button>
              <button
                type="button"
                onClick={() => onApprove(false)}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded border border-amber-500/40 bg-amber-500/10 text-amber-700 hover:bg-amber-500/20 transition-colors"
              >
                <ThumbsDown className="w-3.5 h-3.5" />
                Reject
              </button>
              <span className="text-[10px] text-[hsl(var(--muted-foreground))] ml-1">
                Reject and type refinement guidance in the chat input.
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
