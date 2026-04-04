import { useEffect, useMemo, useState } from "react";
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
interface SessionDetailViewProps {
  session: SessionSummary;
}
export function SessionDetailView({
  session
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
    resume
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
        events={events}
        model={session.context?.model}
        systemBlock={summaryData.summary.length || summaryData.testing.length ? {
          summary: {
            text: summaryData.summary,
            testing: summaryData.testing
          }
        } : undefined} />

      <InputBar mode="detail" sessionContext={session.context} onSubmit={async (query, newContext, mode, attachments) => {
        const mergedContext = { ...session.context, ...newContext };
        await send(query, mergedContext, mode, attachments);
        resume();
      }} onStop={async () => {
        await stop();
        resume();
      }} isRunning={running} isSubmitting={submitting} error={queryError} />
    </>
  );

  const effectiveMaximized = isMobile || isMaximized;
  const workspaceProps = {
    activeTab,
    onTabChange: setActiveTab,
    events: selectedTurn ? selectedTurn.events : events,
    diffContent: selectedFile?.diff,
    filename: selectedFile?.path || selectedFile?.name,
    onContinue: !running ? async () => { await send("Continue the task from where you left off.", session.context ?? {}, undefined, []); resume(); } : undefined,
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
