import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";
import type { RunStatus } from "./InputBar";
import { ConversationTimeline } from "./ConversationTimeline";
import { WorkspacePanel } from "./WorkspacePanel";
import { InputBar } from "./InputBar";
import { useSessionEvents } from "../hooks/useSessionEvents";
import { useSessionUsage } from "../hooks/useSessionUsage";
import { useSessionQuery } from "../hooks/useSessionQuery";
import { useThroughput } from "../hooks/useThroughput";
import { useIsMobile } from "../hooks/useIsMobile";
import { SessionSummary, TurnMeta } from "../types";
import { buildTimeline, getActiveStreamText, getActiveTurn, getLastContext, isRunAccepted, turnHasWidget } from "../utils/timeline";
import { mergeDiffFiles } from "../utils/diff";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { extractSummaryTesting } from "../utils/logs";
import { answerQuestion, approvePlan } from "../api/client";
import type { QuestionAnswerItemPayload } from "../types";
import { useRecoverSession } from "../hooks/useRecoverSession";
import { useForkSession } from "../hooks/useForkSession";
import { RotateCcw, Play } from "lucide-react";
import { Button } from "./ui/button";
import { SessionTriggersSection } from "./triggers/SessionTriggersSection";
import { SessionHeader } from "./SessionHeader";

interface SessionDetailViewProps {
  session: SessionSummary;
  onTitleUpdate?: (sessionId: string, title: string) => void;
  onSessionChange?: () => void;
  onSelectSession?: (sessionId: string) => void;
  // Session-header obligations.
  onBack: () => void;
  onRenameTitle?: (sessionId: string, title: string) => Promise<void>;
  onRegenerateTitle?: (sessionId: string) => Promise<string>;
  onArchive?: (sessionId: string) => void;
  onUnarchive?: (sessionId: string) => void;
  onShare?: (sessionId: string) => void;
  onExport?: (sessionId: string) => void;
  langfuseUrl?: string | null;
}

export function SessionDetailView({
  session,
  onTitleUpdate,
  onSessionChange,
  onSelectSession,
  onBack,
  onRenameTitle,
  onRegenerateTitle,
  onArchive,
  onUnarchive,
  onShare,
  onExport,
  langfuseUrl,
}: SessionDetailViewProps) {
  const isMobile = useIsMobile();
  const [isWorkspaceOpen, setIsWorkspaceOpen] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  const [activeTab, setActiveTab] = useState<"diff" | "logs" | "binding">("logs");
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const {
    events,
    running,
    status: liveStatus,
    title: liveTitle,
    doneReason: liveDoneReason,
    terminated: liveTerminated,
    recoverable: liveRecoverable,
    error: eventsError,
    resume,
    reset: resetEvents,
  } = useSessionEvents(session.session_id);
  const { usage: sessionUsage } = useSessionUsage(session.session_id, events);
  const {
    send,
    stop,
    error: queryError,
    terminated: queryTerminated,
    submitting
  } = useSessionQuery(session.session_id, session.context, running);
  const timeline = useMemo(() => buildTimeline(events), [events]);
  // A session is permanently terminated when any of four signals says so: the
  // `session_terminated` transcript event, the server's own live termination
  // flag, the listing's session status, or a 410 caught by the composer's
  // send/stop path. The composer disables and the recovery affordances suppress
  // once terminated (it can never run again). The live flag is what keeps this
  // correct before the listing has a row to read `status` from.
  const isTerminated = useMemo(
    () =>
      queryTerminated ||
      liveTerminated === true ||
      session.status === "terminated" ||
      events.some((e) => e.type === "session_terminated"),
    [queryTerminated, liveTerminated, session.status, events],
  );
  const sessionFiles = useMemo(
    () => mergeDiffFiles(timeline.flatMap((e) => e.turn?.files ?? [])),
    [timeline]
  );
  const liveTurn = useMemo(() => getActiveTurn(events), [events]);
  const activeTurnId = liveTurn?.id ?? null;
  // Orchestrator cold start: the run is accepted (`run_accepted`) but has not
  // opened its turn, so the transcript has no row to show. Both conditions are
  // load-bearing — the marker proves a run was accepted for THIS session, and
  // `running` proves the server still considers it live, so an abandoned marker
  // (process killed before the sweep settled the run) can't leave a session
  // reading "Starting…" forever. Once the turn opens, `liveTurn` is non-null
  // and the pending beat takes over.
  const isStarting = (running || submitting) && !liveTurn && isRunAccepted(events);
  // Live assistant text streamed from the in-flight turn. Empty
  // string when no deltas have arrived (non-streaming model, or events with
  // no delta stream), so ConversationTimeline falls back to the "Working…" beat.
  const streamingText = useMemo(() => getActiveStreamText(events), [events]);
  const selectedTurn = useMemo(() => {
    if (!selectedTurnId) {
      return null;
    }
    if (liveTurn && liveTurn.id === selectedTurnId) {
      return liveTurn;
    }
    for (const entry of timeline) {
      if (entry.turn?.id === selectedTurnId) {
        return entry.turn;
      }
    }
    return null;
  }, [selectedTurnId, liveTurn, timeline]);
  // Derive effective session context from live events. This is the SINGLE
  // most-recent context event's payload, verbatim — never merged across
  // events. See `getLastContext` for why (mirrors the backend's
  // `_load_last_context`).
  const effectiveContext = useMemo(
    () => getLastContext(events, session.context),
    [events, session.context],
  );
  // The subject this page renders. `session` is the row the sessions listing
  // holds for this id — frequently nothing yet, because that listing summarises
  // EVERY session and takes seconds, while this page's own per-session fetches
  // return the same authoritative facts for THIS one in a fraction of it. So
  // each field is taken from whichever source has it, and the page never waits
  // on the listing to paint. `liveStatus`/`liveDoneReason` stay separate props
  // on the header — its own snapshot-vs-live precedence already covers those.
  const subject: SessionSummary = useMemo(
    () => ({
      ...session,
      // Precedence is INVERTED relative to status here, deliberately: a rename
      // patches the listing optimistically (`applyTitle`), so a non-empty
      // snapshot outranks the server's older projection instead of flickering
      // back to it. The projection fills the gap when the listing has no row.
      title: session.title || liveTitle || "",
      // The server derives this identically (`summarize_session` reads the
      // first event's `ts`), so this is the listing's own value arriving early
      // rather than an invented timestamp. It assumes the transcript window
      // starts at the session's first event, which holds while the initial
      // `/events` fetch is unbounded; bound that fetch and this has to go back
      // to waiting for the listing's value.
      created_at: session.created_at ?? events[0]?.ts,
      recoverable: liveRecoverable ?? session.recoverable,
      // The header already prefers its `context` prop over this field
      // (`liveContext`), but two of its reads go straight to `session.context`
      // (the model chip, the app-jump button). Folding the transcript-derived
      // context in here is what keeps those two working before the listing has
      // a row, instead of teaching the header a third precedence rule.
      context: session.context ?? effectiveContext,
    }),
    [session, liveTitle, liveRecoverable, events, effectiveContext],
  );
  const summaryData = useMemo(() => extractSummaryTesting(events), [events]);
  // Live throughput + phase classification, differenced from the
  // in-flight turn's streamed output over the poll clock. This is the single
  // source of the run's phase label + tok/s rate that RunTelemetry renders.
  const throughput = useThroughput(events, running);
  // Derive a compact run status for the composer's running-state strip.
  // - phase: what the run is doing right now (streaming / reasoning / running
  //   tool / stalled), from `useThroughput`; defaults to "Sending…"/"Running".
  // - agents: count of sub_agents currently in `start` state (no matching `stop`).
  // - tokens: live root-window fill (sessionUsage.root_last_input_tokens) when present.
  // - tokPerSec: live output throughput while streaming.
  // - lastUserTs: time of the last `user` event in this run (resets per turn).
  const runStatus: RunStatus | undefined = useMemo(() => {
    if (!running && !submitting) return undefined;
    let lastUserTs: string | undefined;
    const liveAgents = new Set<string>();
    for (const ev of events) {
      if (ev.type === "user") {
        lastUserTs = ev.ts;
        // A `user` event is a turn boundary — clear so a run of sub-agents
        // from an earlier turn (started, never matched by a `stop`) can't go
        // on inflating the live count on every later turn.
        liveAgents.clear();
      } else if (ev.type === "sub_agent") {
        const p = (ev.payload ?? {}) as Record<string, unknown>;
        const id = String(p.agent_id ?? "");
        if (!id) continue;
        if (p.action === "start") liveAgents.add(id);
        else if (p.action === "stop") liveAgents.delete(id);
      }
    }
    return {
      phase: throughput.phase ?? (submitting ? "Sending…" : "Running"),
      agents: liveAgents.size,
      tokens: sessionUsage?.root_last_input_tokens,
      tokPerSec: throughput.tokPerSec,
      lastUserTs,
    };
  }, [events, running, submitting, sessionUsage, throughput.phase, throughput.tokPerSec]);
  const errorMessage = eventsError || queryError;
  const errorTitle = eventsError ? "Stream error" : "Request error";
  const autoOpenedRef = useRef<string | null>(null);
  const pushedTitleRef = useRef<string | null>(null);
  useEffect(() => {
    setSelectedTurnId(null);
    setIsWorkspaceOpen(false);
    setIsMaximized(false);
    setActiveTab("logs");
    autoOpenedRef.current = null;
    pushedTitleRef.current = null;
  }, [session.session_id]);
  // Auto-open the trace when a session first loads. Fires once per session
  // (guarded by autoOpenedRef). Skipped on mobile where the workspace would
  // obscure the conversation, and skipped when the latest turn rendered a
  // widget so the widget keeps the spotlight.
  //
  // While a run is live, prefer the in-flight turn so the trace panel fills
  // with live logs without a click — the once-per-session guard
  // keeps it from re-opening after the user deliberately closes it.
  useEffect(() => {
    if (isMobile) return;
    if (autoOpenedRef.current === session.session_id) return;
    if (running && liveTurn) {
      autoOpenedRef.current = session.session_id;
      setSelectedTurnId(liveTurn.id);
      setActiveTab("logs");
      setIsWorkspaceOpen(true);
      return;
    }
    if (timeline.length === 0) return;
    for (let i = timeline.length - 1; i >= 0; i--) {
      const turn = timeline[i].turn;
      if (turn) {
        autoOpenedRef.current = session.session_id;
        if (turnHasWidget(timeline, turn.id)) return;
        setSelectedTurnId(turn.id);
        setIsWorkspaceOpen(true);
        return;
      }
    }
  }, [timeline, session.session_id, isMobile, running, liveTurn]);
  // Push the newest `title_update` into the listing cache — ONCE per distinct
  // (session, title), tracked here rather than inferred from `session.title`.
  //
  // ⚠️ This effect was an unbounded render loop, and the shape is worth
  // recognising because nothing about it looks dangerous:
  //
  //  * `onTitleUpdate` arrives as an inline arrow, so its identity changes
  //    every render and the dep array re-runs the effect every render;
  //  * the old guard compared the event's title against `session.title`, which
  //    on a DIRECT visit is the memoised placeholder row (`title: ""`) that
  //    `SessionDetailRoute` mounts while the `O(collection)` listing loads —
  //    so the comparison could not go false no matter what was written;
  //  * and `applyTitle` fabricated an empty list when the listing had not
  //    loaded, so the write both changed cache data (re-rendering the app,
  //    re-entering the effect) and guaranteed the row stayed missing.
  //
  // A guard must therefore key off what this effect ALREADY DID, never off
  // state some other layer is expected to echo back. This ref is the fix that
  // was measured to stop the loop on its own; `useSessions` separately stopped
  // fabricating the list, which is what let the placeholder row ever resolve.
  useEffect(() => {
    if (!onTitleUpdate) return;
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      if (ev.type === "title_update") {
        const payload = ev.payload as { title?: string } | undefined;
        const title = payload?.title;
        if (
          typeof title === "string" &&
          title &&
          title !== session.title &&
          title !== pushedTitleRef.current
        ) {
          pushedTitleRef.current = title;
          onTitleUpdate(session.session_id, title);
        }
        break;
      }
    }
  }, [events, onTitleUpdate, session.session_id, session.title]);
  const handleShowTrace = (turn: TurnMeta) => {
    setSelectedTurnId(turn.id);
    setActiveTab("logs");
    setIsWorkspaceOpen(true);
  };
  const handleOpenFiles = (turn: TurnMeta) => {
    setSelectedTurnId(turn.id);
    setActiveTab("diff");
    setIsWorkspaceOpen(true);
  };
  const handleShowLiveTrace = () => {
    if (!liveTurn) {
      return;
    }
    setSelectedTurnId(liveTurn.id);
    setActiveTab("logs");
    setIsWorkspaceOpen(true);
  };
  const handleApprovePlan = useCallback(
    async (approved: boolean) => {
      try {
        await approvePlan(session.session_id, approved);
      } catch (err) {
        console.error("Failed to submit plan decision", err);
      } finally {
        // A plan approval re-engages the loop the same way submit/stop do —
        // without this the poll can be sitting stopped (nothing `running` at
        // the time it last checked) and never wake up to see the new turn.
        resume();
      }
    },
    [session.session_id, resume],
  );
  const handleAnswerQuestion = useCallback(
    async (
      callId: string,
      callToken: string,
      answers: QuestionAnswerItemPayload[],
      notes?: string,
    ) => {
      try {
        return await answerQuestion(session.session_id, callId, {
          call_token: callToken,
          answers,
          // Omitted entirely when blank — the server contract forbids an empty
          // string, and a note nobody typed is not a note.
          ...(notes ? { notes } : {}),
        });
      } finally {
        // Same dead-end as plan approval: answering unblocks the loop, so
        // wake the poll rather than leaving it parked until a manual refresh.
        resume();
      }
    },
    [session.session_id, resume],
  );
  const recover = useRecoverSession();
  // Shared post-recovery refresh for a generic (non-wiki) dispatch. A
  // wiki-indexing dispatch navigates away inside the hook, so this never
  // runs for it. Full reset: clear stale events + lastTsRef so the next
  // poll fetches the authoritative transcript from scratch (the backend
  // deletes old events on retry / stale recovery attempts on continue, so a
  // merge-based resume would show orphaned events until hard-refresh), then
  // re-fetch the session list so the SessionHeader StatusBadge flips failed→running.
  const onGenericRecover = useCallback(() => {
    resetEvents();
    onSessionChange?.();
  }, [resetEvents, onSessionChange]);
  // The same three-condition guard (`running` / no session id / a recovery
  // already in flight) repeats verbatim in triggerRecover, handleRetryFrom,
  // and handleEditAndRegenerate below — each call site otherwise builds a
  // differently-shaped mutation payload, so a shared guard function would
  // just move the condition without removing a real duplication. Left as-is;
  // worth revisiting if a fourth call site appears.
  // `model` is the deliberate pick from the failure card's picker. The header
  // affordance has no picker and passes none, which must still resolve to the
  // session's own model — omitting it lets the server fall back to config
  // policy, so a recovery would silently run on a different model than the
  // turn it is recovering.
  const triggerRecover = (action: "retry" | "continue", model?: string) => {
    if (running || !session.session_id || recover.isPending) return;
    recover.mutate({
      sessionId: session.session_id,
      action,
      model: model ?? effectiveContext?.model,
      onGeneric: onGenericRecover,
    });
  };
  const handleRetryFrom = (fromTs: string) => {
    if (running || !session.session_id || recover.isPending) return;
    recover.mutate({
      sessionId: session.session_id,
      action: "retry",
      fromTs,
      model: effectiveContext?.model,
      onGeneric: onGenericRecover,
    });
  };
  const fork = useForkSession();
  // Shared by "Branch in new chat" (fromTs given) and "Fork session" (whole
  // transcript, fromTs omitted) — one fork operation, one handler, so the
  // two menu items can never drift apart in behavior or error handling.
  const handleFork = (fromTs?: string) => {
    if (running || !session.session_id || fork.isPending) return;
    fork.mutate({
      sessionId: session.session_id,
      fromTs,
      model: effectiveContext?.model,
      onSuccess: (result) => {
        onSessionChange?.();
        onSelectSession?.(result.session_id);
      },
    });
  };
  const handleEditAndRegenerate = (fromTs: string, newText: string) => {
    if (running || !session.session_id || recover.isPending) return;
    recover.mutate({
      sessionId: session.session_id,
      action: "retry",
      fromTs,
      editedText: newText,
      model: effectiveContext?.model,
      onGeneric: onGenericRecover,
    });
  };

  // The ONE failure card allowed to offer live Retry/Continue: the newest
  // `run_failed` entry, and only while nothing has superseded it. A later
  // assistant turn means the session recovered, so its failure is history —
  // every other failure card in the transcript renders read-only.
  const recoverableFailureId = useMemo(() => {
    if (running || submitting || isTerminated) return undefined;
    for (let i = timeline.length - 1; i >= 0; i -= 1) {
      const entry = timeline[i];
      if (entry.role === "assistant") return undefined;
      if (entry.role === "run_failed") return entry.id;
    }
    return undefined;
  }, [timeline, running, submitting, isTerminated]);

  // Header-level recovery affordance, gated by the backend's authoritative
  // ``session.recoverable`` flag. Its one genuine job is the case no failure
  // card can cover: a session killed mid-call that never wrote a completion
  // event, so opening /s/<id> on a crashed session still offers recovery.
  // When a live failure card is on screen this defers to it — one affordance.
  const showHeaderRecovery =
    Boolean(subject.recoverable) &&
    !running &&
    !submitting &&
    !isTerminated &&
    !recoverableFailureId;

  const conversationPanel = (
    <>
      {errorMessage && <div className="px-6 pt-4">
        <Alert variant="destructive">
          <AlertTitle>{errorTitle}</AlertTitle>
          <AlertDescription>{errorMessage}</AlertDescription>
        </Alert>
      </div>}

      {!isTerminated && <SessionTriggersSection sessionId={session.session_id} />}

      {showHeaderRecovery && (
        <div className="px-6 pt-4">
          <div className="flex items-center justify-between gap-3 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 px-3 py-2">
            <p className="text-xs text-[hsl(var(--muted-foreground))] min-w-0">
              This session stopped before finishing. Continue to resume with its
              context intact, or restart the last turn.
            </p>
            <div className="flex gap-2 shrink-0">
              <Button
                variant="neutral"
                size="sm"
                tone="warn"
                leadingIcon={<Play className="w-3 h-3" />}
                onClick={() => triggerRecover("continue")}
                title="Resume this session with its context intact"
              >
                Continue
              </Button>
              <Button
                variant="neutral"
                size="sm"
                tone="info"
                leadingIcon={<RotateCcw className="w-3 h-3" />}
                onClick={() => triggerRecover("retry")}
                title="Restart the last turn from scratch"
              >
                Restart
              </Button>
            </div>
          </div>
        </div>
      )}

      <ConversationTimeline
        timeline={timeline}
        onShowTrace={handleShowTrace}
        onOpenFiles={handleOpenFiles}
        activeTurnId={activeTurnId}
        isRunning={running || submitting}
        isStarting={isStarting}
        streamingText={streamingText}
        onShowActiveTrace={handleShowLiveTrace}
        activePhase={throughput.phase}
        activeTokPerSec={throughput.tokPerSec}
        activeStepStartTs={throughput.stepStartTs}
        onApprovePlan={handleApprovePlan}
        onAnswerQuestion={handleAnswerQuestion}
        onRetryFrom={handleRetryFrom}
        onForkFrom={handleFork}
        onForkSession={() => handleFork()}
        onEditAndRegenerate={handleEditAndRegenerate}
        onRecover={triggerRecover}
        recoverableFailureId={recoverableFailureId}
        model={effectiveContext?.model}
        fallbackModels={effectiveContext?.fallback_models}
        sessionUsage={sessionUsage}
        systemBlock={summaryData.summary.length || summaryData.testing.length ? {
          summary: {
            text: summaryData.summary,
            testing: summaryData.testing
          }
        } : undefined} />

      <div className="relative z-10">
        <InputBar mode="detail" sessionId={session.session_id} sessionContext={effectiveContext} onSubmit={async (query, newContext, mode, attachments) => {
          // InputBar emits exactly the overrides this turn may declare. Replaying
          // the persisted context here resurrects purpose-bound fields it
          // deliberately omitted (for example an app's project binding).
          await send(query, newContext, mode, attachments);
          resume();
        }} onStop={async () => {
          await stop();
          resume();
        }} isRunning={running} isSubmitting={submitting} error={queryError} runStatus={runStatus} terminated={isTerminated} />
      </div>
    </>
  );

  // The live spinner anchors users to the running edge — but only when
  // they're actually viewing it. If the user has scrolled back to a
  // completed trace while a new turn is running, swap the spinner for a
  // lightweight "jump to live" hint (handled inside LogsView). Treating
  // the no-selection case (rolling stream) as "live" since that view
  // already includes the active turn's events.
  const isViewingLive =
    !selectedTurn || (liveTurn !== null && selectedTurn.id === liveTurn.id);

  const effectiveMaximized = isMobile || isMaximized;
  const workspaceProps = {
    activeTab,
    onTabChange: setActiveTab,
    events: selectedTurn ? selectedTurn.events : events,
    sessionId: session.session_id,
    selectedTurn: selectedTurn ?? null,
    sessionFiles,
    onRetry: !running ? (m?: string) => triggerRecover("retry", m) : undefined,
    onContinue: !running ? (m?: string) => triggerRecover("continue", m) : undefined,
    model: effectiveContext?.model,
    fallbackModels: effectiveContext?.fallback_models,
    isRunning: running || submitting,
    runStatus,
    isViewingLive,
    onShowLiveTrace: liveTurn ? handleShowLiveTrace : undefined,
  };

  let body: ReactNode;
  if (isWorkspaceOpen && effectiveMaximized) {
    body = (
      <div className="flex flex-col h-full overflow-hidden">
        <WorkspacePanel
          {...workspaceProps}
          onClose={() => { setIsWorkspaceOpen(false); setIsMaximized(false); }}
          isMaximized
          onToggleMaximize={isMobile ? undefined : () => setIsMaximized(false)}
        />
      </div>
    );
  } else if (isWorkspaceOpen) {
    body = (
      <PanelGroup orientation="horizontal" className="flex-1 overflow-hidden h-full bg-[hsl(var(--background))]">
        <Panel id="conversation" defaultSize="70%" minSize="25%" maxSize="75%" className="flex flex-col h-full bg-[hsl(var(--background))]">
          {conversationPanel}
        </Panel>
        <PanelResizeHandle className="pane-rail relative flex w-3 items-stretch justify-center bg-transparent cursor-col-resize group">
          <span className="self-stretch w-px bg-[hsl(var(--border))] group-hover:bg-[hsl(var(--primary))]/55 group-active:bg-[hsl(var(--primary))] transition-colors" aria-hidden />
        </PanelResizeHandle>
        <Panel id="workspace" minSize="25%" className="h-full min-w-0 bg-[hsl(var(--surface))]">
          <WorkspacePanel
            {...workspaceProps}
            onClose={() => setIsWorkspaceOpen(false)}
            isMaximized={false}
            onToggleMaximize={() => setIsMaximized(true)}
          />
        </Panel>
      </PanelGroup>
    );
  } else {
    body = (
      <div className="flex flex-1 overflow-hidden h-full bg-[hsl(var(--background))]">
        <div className="flex flex-col h-full w-full max-w-4xl lg:max-w-5xl xl:max-w-6xl mx-auto">
          {conversationPanel}
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col bg-[hsl(var(--background))]">
      <SessionHeader
        session={subject}
        context={effectiveContext}
        usage={sessionUsage}
        isTerminated={isTerminated}
        liveStatus={liveStatus}
        liveDoneReason={liveDoneReason}
        onBack={onBack}
        onRenameTitle={onRenameTitle}
        onRegenerateTitle={onRegenerateTitle}
        onArchive={onArchive}
        onUnarchive={onUnarchive}
        onShare={onShare}
        onExport={onExport}
        langfuseUrl={langfuseUrl}
      />
      <div className="min-h-0 flex-1">{body}</div>
    </div>
  );
}
