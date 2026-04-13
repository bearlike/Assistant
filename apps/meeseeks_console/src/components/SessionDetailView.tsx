import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";
import { ConversationTimeline } from "./ConversationTimeline";
import { WorkspacePanel } from "./WorkspacePanel";
import { InputBar } from "./InputBar";
import { useSessionEvents } from "../hooks/useSessionEvents";
import { useSessionQuery } from "../hooks/useSessionQuery";
import { useIsMobile } from "../hooks/useIsMobile";
import { SessionContext, SessionSummary, TurnMeta } from "../types";
import { buildTimeline, getActiveTurn } from "../utils/timeline";
import { mergeDiffFiles } from "../utils/diff";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { extractSummaryTesting } from "../utils/logs";
import { approvePlan, recoverSession, forkSession } from "../api/client";
import { RotateCcw, Play } from "lucide-react";
import { Button } from "./ui/Button";
interface SessionDetailViewProps {
  session: SessionSummary;
  onTitleUpdate?: (sessionId: string, title: string) => void;
  onSessionChange?: () => void;
  onSelectSession?: (sessionId: string) => void;
}

export function SessionDetailView({
  session,
  onTitleUpdate,
  onSessionChange,
  onSelectSession,
}: SessionDetailViewProps) {
  const isMobile = useIsMobile();
  const [isWorkspaceOpen, setIsWorkspaceOpen] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  const [activeTab, setActiveTab] = useState<"diff" | "logs">("logs");
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);
  const {
    events,
    running,
    error: eventsError,
    resume,
    reset: resetEvents,
  } = useSessionEvents(session.session_id);
  const {
    send,
    stop,
    error: queryError,
    submitting
  } = useSessionQuery(session.session_id, session.context, running);
  const timeline = useMemo(() => buildTimeline(events), [events]);
  const sessionFiles = useMemo(
    () => mergeDiffFiles(timeline.flatMap((e) => e.turn?.files ?? [])),
    [timeline]
  );
  const liveTurn = useMemo(() => getActiveTurn(events), [events]);
  const activeTurnId = liveTurn?.id ?? null;
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
  // Derive effective session context from live events. Context events are
  // emitted before each query and on plan approval, so they carry the most
  // recent model, mode, project, etc. Merging them keeps InputBar in sync
  // without waiting for a full session-list refresh.
  const effectiveContext = useMemo(() => {
    let ctx: SessionContext | undefined = session.context;
    for (const ev of events) {
      if (ev.type === "context") {
        ctx = { ...ctx, ...(ev.payload as Partial<SessionContext>) };
      }
    }
    return ctx;
  }, [events, session.context]);
  const summaryData = useMemo(() => extractSummaryTesting(events), [events]);
  const errorMessage = eventsError || queryError;
  const errorTitle = eventsError ? "Polling error" : "Request error";
  const autoOpenedRef = useRef<string | null>(null);
  useEffect(() => {
    setSelectedTurnId(null);
    setIsWorkspaceOpen(false);
    setIsMaximized(false);
    setActiveTab("logs");
    autoOpenedRef.current = null;
  }, [session.session_id]);
  // Auto-open the latest completed trace when a session first loads.
  // Fires once per session (guarded by autoOpenedRef). Skipped on mobile
  // where the workspace would obscure the conversation.
  useEffect(() => {
    if (isMobile) return;
    if (autoOpenedRef.current === session.session_id) return;
    if (timeline.length === 0) return;
    for (let i = timeline.length - 1; i >= 0; i--) {
      const turn = timeline[i].turn;
      if (turn) {
        setSelectedTurnId(turn.id);
        setIsWorkspaceOpen(true);
        autoOpenedRef.current = session.session_id;
        return;
      }
    }
  }, [timeline, session.session_id, isMobile]);
  useEffect(() => {
    if (!onTitleUpdate) return;
    for (let i = events.length - 1; i >= 0; i--) {
      const ev = events[i];
      if (ev.type === "title_update") {
        const payload = ev.payload as { title?: string } | undefined;
        const title = payload?.title;
        if (typeof title === "string" && title && title !== session.title) {
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
      }
    },
    [session.session_id],
  );
  const triggerRecover = async (action: "retry" | "continue") => {
    if (running || !session.session_id) return;
    await recoverSession(session.session_id, action);
    // Full reset: clear stale events + lastTsRef so the next poll
    // fetches the authoritative transcript from scratch. The backend
    // deletes old events on retry (time-travel) and stale recovery
    // attempts on continue (stitch), so a merge-based resume would
    // show orphaned events until hard-refresh.
    resetEvents();
    // Re-fetch session list so the NavBar's StatusBadge updates from
    // "failed" to "running" without requiring a hard refresh.
    onSessionChange?.();
  };
  const handleRetryFrom = async (fromTs: string) => {
    if (running || !session.session_id) return;
    await recoverSession(session.session_id, "retry", fromTs, undefined, effectiveContext?.model);
    resetEvents();
    onSessionChange?.();
  };
  const handleForkFrom = async (fromTs: string) => {
    if (running || !session.session_id) return;
    try {
      const result = await forkSession(session.session_id, {
        fromTs,
        model: effectiveContext?.model ?? undefined,
      });
      onSessionChange?.();
      onSelectSession?.(result.session_id);
    } catch {
      // fork failed — silently ignore, notification will surface via API
    }
  };
  const handleEditAndRegenerate = async (fromTs: string, newText: string) => {
    if (running || !session.session_id) return;
    await recoverSession(session.session_id, "retry", fromTs, newText, effectiveContext?.model);
    resetEvents();
    onSessionChange?.();
  };

  // Surface the last recoverable failure inline in the conversation so users
  // never have to open the Logs panel to retry. Walks backwards to find the
  // most recent completion event; recoverable iff the run is idle and the
  // reason is ``error`` or ``max_steps_reached``.
  const lastRecoverableFailure = useMemo(() => {
    if (running || submitting) return null;
    for (let i = events.length - 1; i >= 0; i -= 1) {
      const ev = events[i];
      if (ev.type !== "completion") continue;
      const payload = (ev.payload ?? {}) as {
        done_reason?: string | null;
        error?: string;
        last_error?: string;
      };
      const reason = (payload.done_reason ?? "").toLowerCase();
      if (reason !== "error" && reason !== "max_steps_reached") return null;
      return {
        reason,
        error: payload.error ?? payload.last_error ?? "",
      };
    }
    return null;
  }, [events, running, submitting]);

  const conversationPanel = (
    <>
      {errorMessage && <div className="px-6 pt-4">
        <Alert variant="destructive">
          <AlertTitle>{errorTitle}</AlertTitle>
          <AlertDescription>{errorMessage}</AlertDescription>
        </Alert>
      </div>}

      <ConversationTimeline
        timeline={timeline}
        onShowTrace={handleShowTrace}
        onOpenFiles={handleOpenFiles}
        activeTurnId={activeTurnId}
        isRunning={running || submitting}
        onShowActiveTrace={handleShowLiveTrace}
        onApprovePlan={handleApprovePlan}
        onRetryFrom={handleRetryFrom}
        onForkFrom={handleForkFrom}
        onEditAndRegenerate={handleEditAndRegenerate}
        events={events}
        model={effectiveContext?.model}
        systemBlock={summaryData.summary.length || summaryData.testing.length ? {
          summary: {
            text: summaryData.summary,
            testing: summaryData.testing
          }
        } : undefined} />

      {lastRecoverableFailure && (
        <div className="px-6 py-3 border-t border-[hsl(var(--border))]">
          <div className="rounded-md border border-red-500/30 bg-red-500/5 p-3">
            <div className="flex items-center justify-between gap-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-red-500">
                  {lastRecoverableFailure.reason === "error"
                    ? "Run failed"
                    : "Task interrupted — step limit reached"}
                </p>
                {lastRecoverableFailure.error && (
                  <p className="mt-1 text-xs font-mono text-red-500/80 break-words">
                    {lastRecoverableFailure.error}
                  </p>
                )}
              </div>
              <div className="flex gap-2 shrink-0">
                <Button
                  variant="neutral"
                  size="sm"
                  tone="info"
                  leadingIcon={<RotateCcw className="w-3 h-3" />}
                  onClick={() => triggerRecover("retry")}
                  title="Re-run the last user query"
                >
                  Retry
                </Button>
                <Button
                  variant="neutral"
                  size="sm"
                  tone="warn"
                  leadingIcon={<Play className="w-3 h-3" />}
                  onClick={() => triggerRecover("continue")}
                  title="Resume the session and let the agent recover"
                >
                  Continue
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="relative z-10">
        <InputBar mode="detail" sessionContext={effectiveContext} onSubmit={async (query, newContext, mode, attachments) => {
          const mergedContext = { ...effectiveContext, ...newContext };
          await send(query, mergedContext, mode, attachments);
          resume();
        }} onStop={async () => {
          await stop();
          resume();
        }} isRunning={running} isSubmitting={submitting} error={queryError} />
      </div>
    </>
  );

  const effectiveMaximized = isMobile || isMaximized;
  const workspaceProps = {
    activeTab,
    onTabChange: setActiveTab,
    events: selectedTurn ? selectedTurn.events : events,
    sessionId: session.session_id,
    selectedTurn: selectedTurn ?? null,
    sessionFiles,
    onRetry: !running ? () => triggerRecover("retry") : undefined,
    onContinue: !running ? () => triggerRecover("continue") : undefined,
  };

  if (isWorkspaceOpen && effectiveMaximized) {
    return (
      <div className="flex flex-col h-full overflow-hidden">
        <WorkspacePanel
          {...workspaceProps}
          onClose={() => { setIsWorkspaceOpen(false); setIsMaximized(false); }}
          isMaximized
          onToggleMaximize={isMobile ? undefined : () => setIsMaximized(false)}
        />
      </div>
    );
  }

  if (isWorkspaceOpen) {
    return (
      <PanelGroup orientation="horizontal" className="flex-1 overflow-hidden h-full">
        <Panel id="conversation" defaultSize="40%" minSize="25%" maxSize="75%" className="flex flex-col h-full">
          {conversationPanel}
        </Panel>
        <PanelResizeHandle className="w-1.5 bg-[hsl(var(--border))] hover:bg-[hsl(var(--primary))]/60 active:bg-[hsl(var(--primary))] transition-colors cursor-col-resize" />
        <Panel id="workspace" minSize="25%" className="h-full min-w-0">
          <WorkspacePanel
            {...workspaceProps}
            onClose={() => setIsWorkspaceOpen(false)}
            isMaximized={false}
            onToggleMaximize={() => setIsMaximized(true)}
          />
        </Panel>
      </PanelGroup>
    );
  }

  return (
    <div className="flex flex-1 overflow-hidden h-full">
      <div className="flex flex-col h-full w-full max-w-4xl mx-auto">
        {conversationPanel}
      </div>
    </div>
  );
}
