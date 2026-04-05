import { useCallback, useEffect, useMemo, useState } from "react";
import { Group as PanelGroup, Panel, Separator as PanelResizeHandle } from "react-resizable-panels";
import { ConversationTimeline } from "./ConversationTimeline";
import { WorkspacePanel } from "./WorkspacePanel";
import { InputBar } from "./InputBar";
import { useSessionEvents } from "../hooks/useSessionEvents";
import { useSessionQuery } from "../hooks/useSessionQuery";
import { useIsMobile } from "../hooks/useIsMobile";
import { DiffFile, SessionSummary, TurnMeta } from "../types";
import { buildTimeline, getActiveTurn } from "../utils/timeline";
import { Alert, AlertDescription, AlertTitle } from "./ui/alert";
import { extractSummaryTesting } from "../utils/logs";
import { approvePlan, recoverSession } from "../api/client";
import { RotateCcw, Play } from "lucide-react";
interface SessionDetailViewProps {
  session: SessionSummary;
  onTitleUpdate?: (sessionId: string, title: string) => void;
  onSessionChange?: () => void;
}
export function SessionDetailView({
  session,
  onTitleUpdate,
  onSessionChange,
}: SessionDetailViewProps) {
  const isMobile = useIsMobile();
  const [isWorkspaceOpen, setIsWorkspaceOpen] = useState(false);
  const [isMaximized, setIsMaximized] = useState(false);
  const [activeTab, setActiveTab] = useState<"diff" | "logs">("logs");
  const [selectedFile, setSelectedFile] = useState<DiffFile | null>(null);
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
  const summaryData = useMemo(() => extractSummaryTesting(events), [events]);
  const errorMessage = eventsError || queryError;
  const errorTitle = eventsError ? "Polling error" : "Request error";
  useEffect(() => {
    setSelectedTurnId(null);
    setSelectedFile(null);
    setIsWorkspaceOpen(false);
    setIsMaximized(false);
    setActiveTab("logs");
  }, [session.session_id]);
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
  const handleOpenFiles = (turn: TurnMeta, file?: DiffFile) => {
    setSelectedTurnId(turn.id);
    setActiveTab("diff");
    const nextFile = file || turn.files[0] || null;
    setSelectedFile(nextFile);
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
  useEffect(() => {
    if (!selectedTurn) {
      setSelectedFile(null);
      return;
    }
    if (selectedTurn.files.length === 0) {
      setSelectedFile(null);
      return;
    }
    const selectedKey = selectedFile?.path || selectedFile?.name;
    const hasSelected = Boolean(selectedKey && selectedTurn.files.some((file) => (file.path || file.name) === selectedKey));
    if (!hasSelected) {
      setSelectedFile(selectedTurn.files[0]);
    }
  }, [selectedTurn, selectedFile]);
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
    await recoverSession(session.session_id, "retry", fromTs);
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
        events={events}
        model={session.context?.model}
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
                <button
                  onClick={() => triggerRecover("retry")}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 transition-colors"
                  title="Re-run the last user query"
                >
                  <RotateCcw className="w-3 h-3" />
                  Retry
                </button>
                <button
                  onClick={() => triggerRecover("continue")}
                  className="inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md bg-amber-500/10 text-amber-500 hover:bg-amber-500/20 transition-colors"
                  title="Resume the session and let the agent recover"
                >
                  <Play className="w-3 h-3" />
                  Continue
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <div className="relative z-10">
        <InputBar mode="detail" sessionContext={session.context} onSubmit={async (query, newContext, mode, attachments) => {
          const mergedContext = { ...session.context, ...newContext };
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
    diffContent: selectedFile?.diff,
    filename: selectedFile?.path || selectedFile?.name,
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
