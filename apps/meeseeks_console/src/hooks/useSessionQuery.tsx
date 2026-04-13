import { useCallback, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  interruptStep,
  postQuery,
  sendMessage,
  uploadAttachments,
} from "../api/client";
import { AttachmentPayload, QueryMode, SessionContext } from "../types";
import { logApiError } from "../utils/errors";

type SendVars = {
  query: string;
  contextOverride?: SessionContext;
  mode?: QueryMode;
  attachments?: File[];
};

export function useSessionQuery(
  sessionId?: string,
  context?: SessionContext,
  isRunning = false,
) {
  const [error, setError] = useState<string | null>(null);

  const sendM = useMutation({
    mutationFn: async (vars: SendVars) => {
      if (!sessionId) return;
      if (isRunning) {
        await sendMessage(sessionId, vars.query);
        return;
      }
      const payloadContext = vars.contextOverride ?? context;
      let attachmentRecords: AttachmentPayload[] | undefined;
      if (vars.attachments && vars.attachments.length > 0) {
        attachmentRecords = await uploadAttachments(sessionId, vars.attachments);
      }
      await postQuery(
        sessionId,
        vars.query,
        payloadContext,
        vars.mode,
        attachmentRecords,
      );
    },
    onError: (err) => {
      setError(logApiError("postQuery", err));
    },
  });

  const stopM = useMutation({
    mutationFn: async () => {
      if (!sessionId) return;
      await postQuery(sessionId, "/terminate");
    },
    onError: (err) => {
      setError(logApiError("terminateSession", err));
    },
  });

  const interruptM = useMutation({
    mutationFn: async () => {
      if (!sessionId) return;
      await interruptStep(sessionId);
    },
    onError: (err) => {
      setError(logApiError("interruptStep", err));
    },
  });

  const send = useCallback(
    async (
      query: string,
      contextOverride?: SessionContext,
      mode?: QueryMode,
      attachments?: File[],
    ) => {
      setError(null);
      try {
        await sendM.mutateAsync({ query, contextOverride, mode, attachments });
      } catch {
        // error already captured via onError
      }
    },
    [sendM],
  );

  const stop = useCallback(async () => {
    setError(null);
    try {
      await stopM.mutateAsync();
    } catch {
      // captured via onError
    }
  }, [stopM]);

  const interrupt = useCallback(async () => {
    setError(null);
    try {
      await interruptM.mutateAsync();
    } catch {
      // captured via onError
    }
  }, [interruptM]);

  const clearError = useCallback(() => setError(null), []);

  return {
    send,
    stop,
    interrupt,
    error,
    submitting: sendM.isPending || stopM.isPending || interruptM.isPending,
    clearError,
  };
}
