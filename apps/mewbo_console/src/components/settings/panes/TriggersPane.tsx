/**
 * TriggersPane — the reverse-invocation trigger management surface, hosted in
 * the Automation facet (`../panes.ts`).
 *
 * A pane in the facet registry, so it takes ZERO props and fetches its own
 * data via `useTriggers`/`useSessions` (TanStack cache, shared by queryKey).
 * Migrated from the standalone `/triggers` page — chrome (page
 * width/padding/`<h1>`) is stripped; the Settings shell supplies both, and the
 * Automation facet's own heading + blurb (`settings/facets.ts`) replace the
 * old page title. The backend policy ceilings (max armed per session, expiry,
 * cron minimum interval) render as the schema-driven "Triggers" section
 * directly below this pane in the SAME facet (`TriggersConfig.x-group`) — see
 * this pane's `<SettingsCard description>` for the pointer.
 *
 * Filtering stays entirely client-side over one `limit=200` fetch — see
 * console CLAUDE.md "Triggers + terminated sessions" for why (small dataset,
 * local filtering beats a refetch per toggle, dropdowns only offer values
 * actually present). Do not "optimize" this into server-side query params.
 *
 * The `?session=` deep link is load-bearing: `SessionTriggersSection` (the
 * armed-triggers strip on a session view) and the retired `/triggers` route's
 * redirect both link to `/settings?facet=automation&session=<id>`. This pane
 * reads that param off wouter's `useSearchParams()` exactly as the old
 * `TriggersView` did; the Settings shell preserves unrelated query params
 * (like `facet`) when it writes its own, so both coexist.
 */
import { useEffect, useMemo, useState } from "react";
import { useLocation, useSearchParams } from "wouter";
import { AlarmClock, RefreshCw } from "lucide-react";
import { cn } from "../../../lib/utils";
import {
  isActiveTrigger,
  type TriggerDTO,
  type TriggerKind,
  type TriggerStatus,
} from "../../../api/triggers";
import {
  useCancelTrigger,
  usePauseResumeTrigger,
  useTriggers,
} from "../../../hooks/useTriggers";
import { useSessions } from "../../../hooks/useSessions";
import type { SessionSummary } from "../../../types";
import { Button } from "../../ui/button";
import { ConfirmDialog } from "../../ui/confirm-dialog";
import { ErrorAlert } from "../../ErrorAlert";
import { TriggerRow } from "../../triggers/TriggerRow";
import { KIND_META, STATUS_META } from "../../triggers/triggerFormat";
import { FilterDropdown } from "../FilterDropdown";
import { SettingsCard } from "../SettingsCard";

// Fetch a generous page and filter client-side: a trigger dashboard is small
// enough that one fetch + local filtering beats a refetch per toggle, and it
// lets the filter dropdowns offer exactly the kinds/statuses/sessions that are
// actually present. The server-side `?kind/status/session_id/limit` filters
// stay the escalation seam if the list ever grows past this.
const FETCH_LIMIT = 200;

const TRIGGERS_HELP =
  "A trigger is how Mewbo starts a session later, on its own, with nobody watching.\n\n" +
  "Normally you open a session and it runs while you are there. A trigger turns that around. " +
  "A session asks to be woken when some moment arrives, and Mewbo wakes it then, whether or " +
  "not anyone is looking. That is what makes it useful for work you want done while you are " +
  "away: a report each morning, a follow-up once the build is green, a reply the moment a " +
  "review lands.\n\n" +
  "There are five kinds, and each one is a different way of saying when. **time.at** fires " +
  "once, at a moment you set. **time.cron** fires again and again, on a repeating schedule. " +
  "**ci.workflow** fires when a CI run you are watching finishes. **forge.pr** fires when a " +
  "pull request changes. **webhook** fires when something outside Mewbo calls in.\n\n" +
  "Triggers are armed by the agent from inside a session, so you never create one here. This " +
  "is where you watch what has been armed, pause anything you want to hold, and cancel what " +
  "you no longer want. The limits it all has to stay inside, such as how many triggers one " +
  "session may arm, how long a trigger lives, and how often a cron schedule may fire, are set " +
  "in the **Triggers** section directly below.";

type Filter<T> = T | "all";

export function TriggersPane() {
  const [, navigate] = useLocation();
  const [searchParams] = useSearchParams();
  const sessionParam = searchParams.get("session");

  const { triggers, loading, fetching, error, refresh } = useTriggers({ limit: FETCH_LIMIT });
  const { sessions, archivedSessions, refreshArchived } = useSessions();
  const pauseResume = usePauseResumeTrigger();
  const cancel = useCancelTrigger();

  const [kindFilter, setKindFilter] = useState<Filter<TriggerKind>>("all");
  const [statusFilter, setStatusFilter] = useState<Filter<TriggerStatus>>("all");
  const [sessionFilter, setSessionFilter] = useState<string>("all");
  const [cancelTarget, setCancelTarget] = useState<TriggerDTO | null>(null);

  // Load archived sessions once so a trigger pointing at an archived session is
  // correctly flagged as orphaned (the active list alone can't see it).
  // `refreshArchived` is a fresh closure each render, so mount-once here rather
  // than depending on it (that would refetch-loop).
  useEffect(() => {
    void refreshArchived();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A deep-link (from a session-view chip, or the retired `/triggers` route's
  // redirect) seeds/overrides the session filter.
  useEffect(() => {
    if (sessionParam) setSessionFilter(sessionParam);
  }, [sessionParam]);

  // Session lookup for row labels + orphan detection (active ∪ archived).
  const sessionMap = useMemo(() => {
    const map = new Map<string, SessionSummary>();
    for (const s of [...sessions, ...archivedSessions]) map.set(s.session_id, s);
    return map;
  }, [sessions, archivedSessions]);

  const isDangling = (t: TriggerDTO): boolean => {
    if (!isActiveTrigger(t)) return false;
    const s = sessionMap.get(t.session_id);
    return Boolean(s && (s.status === "terminated" || s.archived));
  };

  // Filter option sets — only the values actually present.
  const { kindOptions, statusOptions, sessionOptions } = useMemo(() => {
    const kinds = new Set<TriggerKind>();
    const statuses = new Set<TriggerStatus>();
    const sessionIds = new Set<string>();
    for (const t of triggers) {
      kinds.add(t.kind);
      statuses.add(t.status);
      sessionIds.add(t.session_id);
    }
    return {
      kindOptions: [...kinds].map((k) => ({ value: k, label: KIND_META[k]?.label ?? k })),
      statusOptions: [...statuses].map((s) => ({ value: s, label: STATUS_META[s]?.label ?? s })),
      sessionOptions: [...sessionIds].map((id) => ({
        value: id,
        label: sessionMap.get(id)?.title?.trim() || `${id.slice(0, 8)}…`,
      })),
    };
  }, [triggers, sessionMap]);

  const filtered = useMemo(() => {
    return triggers.filter((t) => {
      if (kindFilter !== "all" && t.kind !== kindFilter) return false;
      if (statusFilter !== "all" && t.status !== statusFilter) return false;
      if (sessionFilter !== "all" && t.session_id !== sessionFilter) return false;
      return true;
    });
  }, [triggers, kindFilter, statusFilter, sessionFilter]);

  const busyId = pauseResume.isPending
    ? pauseResume.variables?.id
    : cancel.isPending
      ? cancel.variables
      : undefined;

  const handlePauseResume = (t: TriggerDTO) =>
    pauseResume.mutate({ id: t.id, status: t.status === "paused" ? "armed" : "paused" });
  const confirmCancel = () => {
    if (cancelTarget) cancel.mutate(cancelTarget.id);
    setCancelTarget(null);
  };

  const hasActiveFilter =
    kindFilter !== "all" || statusFilter !== "all" || sessionFilter !== "all";

  return (
    <div className="space-y-4">
      <SettingsCard
        id="settings-triggers"
        title="Reverse-invocation triggers"
        description={TRIGGERS_HELP}
        actions={
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={() => void refresh()}
            aria-label="Refresh triggers"
            title="Refresh triggers"
            className={cn(fetching && "opacity-60")}
          >
            <RefreshCw className={cn("w-4 h-4", fetching && "animate-spin")} />
          </Button>
        }
      >
        {/* Filter toolbar */}
        <div className="flex flex-wrap items-center gap-2">
          <FilterDropdown
            label="Kind"
            value={kindFilter}
            options={kindOptions}
            onChange={(v) => setKindFilter(v as Filter<TriggerKind>)}
          />
          <FilterDropdown
            label="Status"
            value={statusFilter}
            options={statusOptions}
            onChange={(v) => setStatusFilter(v as Filter<TriggerStatus>)}
          />
          <FilterDropdown
            label="Session"
            value={sessionFilter}
            options={sessionOptions}
            onChange={setSessionFilter}
          />
          {hasActiveFilter && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                setKindFilter("all");
                setStatusFilter("all");
                setSessionFilter("all");
              }}
            >
              Clear filters
            </Button>
          )}
          <span className="ml-auto text-xs text-[hsl(var(--muted-foreground))] tabular-nums">
            {filtered.length} of {triggers.length}
          </span>
        </div>

        {/* Body */}
        <div className="mt-4">
          {error ? (
            <ErrorAlert
              title="Couldn't load triggers"
              error={error}
              fallback="Failed to load triggers"
            />
          ) : loading ? (
            <div className="py-16 text-center text-sm text-[hsl(var(--muted-foreground))]">
              Loading triggers…
            </div>
          ) : triggers.length === 0 ? (
            <div className="py-16 text-center">
              <AlarmClock className="w-8 h-8 mx-auto text-[hsl(var(--muted-foreground))]" />
              <p className="mt-3 text-sm font-medium text-[hsl(var(--foreground))]">No triggers yet</p>
              <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))] max-w-sm mx-auto">
                Once an agent asks to be woken later, by a timer, a schedule, a CI run, a
                pull request, or a webhook, the trigger shows up here.
              </p>
            </div>
          ) : filtered.length === 0 ? (
            <div className="py-16 text-center text-sm text-[hsl(var(--muted-foreground))]">
              No triggers match the current filters.
            </div>
          ) : (
            <div className="divide-y divide-[hsl(var(--border-strong))] border-y border-[hsl(var(--border-strong))]">
              {filtered.map((t) => (
                <TriggerRow
                  key={t.id}
                  trigger={t}
                  session={sessionMap.get(t.session_id)}
                  dangling={isDangling(t)}
                  loading={busyId === t.id}
                  onNavigateSession={(id) => navigate(`/s/${encodeURIComponent(id)}`)}
                  onPauseResume={handlePauseResume}
                  onCancel={setCancelTarget}
                  onCancelDangling={(trigger) => cancel.mutate(trigger.id)}
                />
              ))}
            </div>
          )}
        </div>
      </SettingsCard>

      {/* Cancel confirm */}
      <ConfirmDialog
        open={cancelTarget !== null}
        title="Cancel this trigger?"
        description="The trigger stops firing and can't be re-armed. The target session is unaffected. This can't be undone."
        cancelLabel="Keep trigger"
        confirmLabel="Cancel trigger"
        onConfirm={confirmCancel}
        onCancel={() => setCancelTarget(null)}
      />
    </div>
  );
}
