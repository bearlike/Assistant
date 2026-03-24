import { useCallback, useState } from "react";
import { postQuery, sendMessage, interruptStep, uploadAttachments } from "../api/client";
import { AttachmentPayload, QueryMode, SessionContext } from "../types";
import { logApiError } from "../utils/errors";
export function useSessionQuery(sessionId?: string, context?: SessionContext, isRunning = false) {
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const send = useCallback(async (
    query: string,
    contextOverride?: SessionContext,
    mode?: QueryMode,
    attachments?: File[]
  ) => {
    if (!sessionId) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      if (isRunning) {
        await sendMessage(sessionId, query);
      } else {
        const payloadContext = contextOverride ?? context;
        let attachmentRecords: AttachmentPayload[] | undefined;
        if (attachments && attachments.length > 0) {
          attachmentRecords = await uploadAttachments(sessionId, attachments);
        }
        await postQuery(sessionId, query, payloadContext, mode, attachmentRecords);
      }
    } catch (err) {
      const message = logApiError("postQuery", err);
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }, [sessionId, context, isRunning]);
  const stop = useCallback(async () => {
    if (!sessionId) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await postQuery(sessionId, "/terminate");
    } catch (err) {
      const message = logApiError("terminateSession", err);
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }, [sessionId]);
  const interrupt = useCallback(async () => {
    if (!sessionId) {
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      await interruptStep(sessionId);
    } catch (err) {
      const message = logApiError("interruptStep", err);
      setError(message);
    } finally {
      setSubmitting(false);
    }
  }, [sessionId]);
  const clearError = useCallback(() => {
    setError(null);
  }, []);
  return {
    send,
    stop,
    interrupt,
    error,
    submitting,
    clearError
  };
}
