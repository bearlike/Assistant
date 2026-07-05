import { ArrowRight, CheckCircle2, Circle, ListChecks } from 'lucide-react';
import type { TodoItemStatus, TodoMeta } from '../types';

/**
 * Live plan/todo checklist card (Gitea #174) — renders the authoritative
 * `todos` event (#173 schema) as a tri-state checklist. Reuses PlanCard's
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
        className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--primary))]"
        aria-hidden
      />
    );
  }
  return (
    <Circle
      className="w-3.5 h-3.5 shrink-0 mt-0.5 text-[hsl(var(--muted-foreground))]/50"
      aria-hidden
    />
  );
}

export function TodoCard({ todos }: { todos: TodoMeta }) {
  const { items, source } = todos;
  const done = items.filter((i) => i.status === 'completed').length;
  const allDone = done === items.length;
  // Count pill borrows PlanCard's status-badge idiom: emerald once the plan is
  // fully worked, cyan (the "in flight" tone) while there's work remaining.
  const badgeClass = allDone
    ? 'bg-emerald-500/10 text-emerald-700 border-emerald-500/30'
    : 'bg-cyan-500/10 text-cyan-700 border-cyan-500/30';

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <ListChecks
          className={`w-4 h-4 shrink-0 ${allDone ? 'text-emerald-600' : 'text-cyan-600'}`}
        />
        <span className="text-sm font-semibold text-[hsl(var(--foreground))]">
          {source === 'agent' ? 'Agent todos' : 'Plan'}
        </span>
        <span
          className={`text-[10px] font-medium px-2 py-0.5 rounded-full border tabular-nums ${badgeClass}`}
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
