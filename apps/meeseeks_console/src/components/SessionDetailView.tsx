import React, { useEffect, useMemo, useState } from "react";
import { ConversationTimeline } from "./ConversationTimeline";
import { WorkspacePanel } from "./WorkspacePanel";
import { InputBar } from "./InputBar";
import { useSessionEvents } from "../hooks/useSessionEvents";
import { useSessionQuery } from "../hooks/useSessionQuery";
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
  const [isWorkspaceOpen, setIsWorkspaceOpen] = useState(false);
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
  return <div className="flex flex-1 overflow-hidden h-full">
      {/* Left Panel: Conversation */}
      <div className={`flex flex-col h-full transition-all duration-300 ${isWorkspaceOpen ? "w-[40%]" : "w-full max-w-4xl mx-auto"}`}>
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
        systemBlock={summaryData.summary.length || summaryData.testing.length ? {
        summary: {
          text: summaryData.summary,
          testing: summaryData.testing
        }
      } : undefined} />

        <InputBar mode="detail" onSubmit={async (query, context, mode, attachments) => {
        await send(query, context, mode, attachments);
        resume();
      }} onStop={async () => {
        await stop();
        resume();
      }} isRunning={running} isSubmitting={submitting} error={queryError} />
      </div>

      {/* Right Panel: Workspace */}
      {isWorkspaceOpen && <div className="flex-1 h-full min-w-0">
          <WorkspacePanel onClose={() => setIsWorkspaceOpen(false)} activeTab={activeTab} onTabChange={setActiveTab} events={selectedTurn ? selectedTurn.events : events} diffContent={selectedFile?.diff} filename={selectedFile?.path || selectedFile?.name} />
        </div>}
    </div>;
}
