import { ArrowRight, CheckCircle2, Circle, ListChecks } from 'lucide-react';
import type { TodoItemStatus, TodoMeta } from '../types';

/**
 * Live plan/todo checklist card — renders the authoritative
 * `todos` event as a tri-state checklist. Reuses PlanCard's
 * card + badge vocabulary (same left-surface `rounded-lg` card, count pill,
 * lucide iconography) so a plan and its execution state read as one family.
 *
 * Never fabricated: `buildTimeline` drops an empty item list upstream, so this
 * always has ≥1 row. Tri-state mirrors the CLI's dock: ✓ completed
 * de-emphasised, → in-progress accented (the eye lands there), ○ pending muted.
 */
function StatusGlyph({ status }: { status: TodoItemStatus }) {
  if (status === 'completed') {
    return (
      <CheckCircle2
        className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--muted-foreground))]"
        aria-hidden
      />
    );
  }
  if (status === 'in_progress') {
    return (
      <ArrowRight
        className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--primary-text))]"
        aria-hidden
      />
    );
  }
  return (
    <Circle
      className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--muted-foreground))]"
      aria-hidden
    />
  );
}

export function TodoCard({ todos }: { todos: TodoMeta }) {
  const { items, source } = todos;
  const done = items.filter((i) => i.status === 'completed').length;
  const allDone = done === items.length;
  // Count pill borrows PlanCard's status-badge idiom: --success once the plan
  // is fully worked, --info (the "in flight" tone) while there's work
  // remaining — run state is gated by the same rule PlanCard's own
  // badge/icon pair follows, so the two stay one family.
  const badgeClass = allDone
    ? 'bg-[hsl(var(--success)/0.1)] text-[hsl(var(--success))] border-[hsl(var(--success)/0.3)]'
    : 'bg-[hsl(var(--info)/0.1)] text-[hsl(var(--info-text))] border-[hsl(var(--info)/0.3)]';

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <ListChecks
          className={`w-4 h-4 shrink-0 ${allDone ? 'text-[hsl(var(--success))]' : 'text-[hsl(var(--info-text))]'}`}
        />
        <span className="text-sm font-medium text-[hsl(var(--foreground))]">
          {source === 'agent' ? 'Agent todos' : 'Plan'}
        </span>
        <span
          className={`text-2xs font-medium px-2 py-0.5 rounded-full border tabular-nums ${badgeClass}`}
        >
          {done}/{items.length}
        </span>
      </div>
      <ul className="border-t border-[hsl(var(--border))] px-4 py-3 space-y-1.5">
        {items.map((item, i) => (
          <li key={i} className="flex items-start gap-2 text-xs leading-relaxed">
            <StatusGlyph status={item.status} />
            <span
              className={
                item.status === 'completed'
                  ? 'text-[hsl(var(--muted-foreground))] line-through decoration-[hsl(var(--muted-foreground))]/40'
                  : item.status === 'in_progress'
                    ? 'text-[hsl(var(--foreground))] font-medium'
                    : 'text-[hsl(var(--foreground))]/80'
              }
            >
              {item.label}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
