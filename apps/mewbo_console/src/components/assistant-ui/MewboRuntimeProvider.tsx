import { useMemo, type ReactNode } from "react";
import {
  AssistantRuntimeProvider,
  useExternalStoreRuntime,
  type AppendMessage,
  type ExternalStoreThreadData,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import type { SessionSummary } from "@/types";

/**
 * MewboRuntimeProvider — the single seam that binds Mewbo's session state to
 * assistant-ui's runtime, so any descendant can use assistant-ui primitives
 * (`ComposerPrimitive`, `ThreadListPrimitive`, `useComposerRuntime`, …).
 *
 * Atomic by design: it owns exactly one `useExternalStoreRuntime` adapter and
 * receives every collaborator (session lists, run state, action callbacks) by
 * prop injection — it fetches nothing and holds no state of its own. Mount it
 * once, high enough that both the landing and session-detail subtrees sit
 * under it (see `AppLayout`).
 *
 * INCREMENTAL ADOPTION — why `messages` is empty:
 * The bespoke `ConversationTimeline` still owns transcript rendering. So the
 * runtime carries NO messages yet and `convertMessage` is the identity — the
 * composer and thread-list are the first two consumers. When the transcript
 * migrates onto assistant-ui, `convertMessage` becomes the real map from our
 * event shape → `ThreadMessageLike`, and `messages` gets fed the live turns.
 *
 * How a child reaches the runtime: render assistant-ui primitives (or call
 * `useComposerRuntime()` / `useThreadListRuntime()`) anywhere below this
 * provider — no prop-drilling. The composer's send flow lands in `onNew`;
 * Stop lands in `onCancel`; thread-list row clicks land in `onSwitchTo*`.
 */
export type MewboRuntimeProviderProps = {
  /** Active (non-archived) sessions, newest-first, from `useSessions`. */
  sessions: SessionSummary[];
  /** Archived sessions — surfaced as the thread-list's archived group. */
  archivedSessions: SessionSummary[];
  /** The session currently open (`/s/:id`), or null on the landing page. */
  activeSessionId: string | null;
  /** Whether the open session's run is in flight — flows to `thread.isRunning`. */
  isRunning: boolean;
  /** Composer submit: create-and-run (landing) or steer (detail). Required by the adapter. */
  onNew: (message: AppendMessage) => Promise<void>;
  /** Composer Stop — cancels the in-flight run. */
  onCancel?: () => Promise<void>;
  /** Thread-list row click → navigate to that session. */
  onSwitchToThread: (threadId: string) => void | Promise<void>;
  /** Thread-list "new" → go to the landing composer. */
  onSwitchToNewThread: () => void | Promise<void>;
  /** Inline rename of a thread-list row. */
  onRename?: (threadId: string, newTitle: string) => void | Promise<void>;
  /** Archive a thread-list row. */
  onArchive?: (threadId: string) => void | Promise<void>;
  children: ReactNode;
};

// Stable references so the adapter doesn't read a "changed" messages array on
// every render while the transcript stays bespoke.
const EMPTY_MESSAGES: readonly ThreadMessageLike[] = [];
const identityConvert = (message: ThreadMessageLike): ThreadMessageLike => message;

export function MewboRuntimeProvider({
  sessions,
  archivedSessions,
  activeSessionId,
  isRunning,
  onNew,
  onCancel,
  onSwitchToThread,
  onSwitchToNewThread,
  onRename,
  onArchive,
  children,
}: MewboRuntimeProviderProps) {
  const threads = useMemo<ExternalStoreThreadData<"regular">[]>(
    () =>
      sessions.map((s) => ({
        status: "regular",
        id: s.session_id,
        title: s.title || undefined,
      })),
    [sessions],
  );

  const archivedThreads = useMemo<ExternalStoreThreadData<"archived">[]>(
    () =>
      archivedSessions.map((s) => ({
        status: "archived",
        id: s.session_id,
        title: s.title || undefined,
      })),
    [archivedSessions],
  );

  const runtime = useExternalStoreRuntime<ThreadMessageLike>({
    messages: EMPTY_MESSAGES,
    convertMessage: identityConvert,
    isRunning,
    onNew,
    onCancel,
    adapters: {
      threadList: {
        threadId: activeSessionId ?? undefined,
        threads,
        archivedThreads,
        onSwitchToThread,
        onSwitchToNewThread,
        onRename,
        onArchive,
      },
    },
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      {children}
    </AssistantRuntimeProvider>
  );
}
