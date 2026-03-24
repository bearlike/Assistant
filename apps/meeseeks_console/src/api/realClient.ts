import {
  AttachmentPayload,
  AttachmentRecord,
  EventRecord,
  NotificationItem,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  ShareRecord
} from "../types";
import { ApiClient, ApiConfig, SkillSummary, ToolSummary } from "./contracts";

function withBase(baseUrl: string, path: string) {
  if (!baseUrl) {
    return path;
  }
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

function authHeaders(apiKey?: string): HeadersInit {
  return apiKey ? { "X-API-Key": apiKey } : {};
}

function headers(apiKey?: string): HeadersInit {
  return {
    ...authHeaders(apiKey),
    "Content-Type": "application/json"
  };
}

async function handleJson<T>(response: Response): Promise<T> {
  const text = await response.text();
  const trimmed = text.trim();
  let data: unknown;
  if (trimmed) {
    try {
      data = JSON.parse(trimmed);
    } catch {
      data = undefined;
    }
  } else {
    data = undefined;
  }

  if (!response.ok) {
    let message = "";
    if (data && typeof data === "object" && "message" in data) {
      const maybeMessage = (data as { message?: unknown }).message;
      if (typeof maybeMessage === "string") {
        message = maybeMessage;
      }
    }
    if (!message && data !== undefined) {
      try {
        message = JSON.stringify(data);
      } catch {
        message = String(data);
      }
    }
    if (!message) {
      message = text;
    }
    throw new Error(message || `Request failed: ${response.status}`);
  }

  if (data !== undefined) {
    return data as T;
  }
  return {} as T;
}

export function createRealClient(config: ApiConfig): ApiClient {
  const baseUrl = config.baseUrl || "";
  const apiKey = config.apiKey || "";

  return {
    async listSessions(includeArchived = false): Promise<SessionSummary[]> {
      const params = includeArchived ? "?include_archived=1" : "";
      const response = await fetch(withBase(baseUrl, `/api/sessions${params}`), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ sessions: SessionSummary[] }>(response);
      return payload.sessions;
    },

    async createSession(context?: SessionContext): Promise<string> {
      const response = await fetch(withBase(baseUrl, "/api/sessions"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ context })
      });
      const payload = await handleJson<{ session_id: string }>(response);
      return payload.session_id;
    },

    async postQuery(
      sessionId: string,
      query: string,
      context?: SessionContext,
      mode?: QueryMode,
      attachments?: AttachmentPayload[]
    ): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/query`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ query, context, mode, attachments })
        }
      );
      await handleJson(response);
    },

    async fetchEvents(
      sessionId: string,
      after?: string
    ): Promise<{ events: EventRecord[]; running: boolean }> {
      const params = after ? `?after=${encodeURIComponent(after)}` : "";
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/events${params}`),
        { headers: headers(apiKey) }
      );
      const payload = await handleJson<{
        events: EventRecord[];
        running: boolean;
      }>(response);
      return payload;
    },

    async archiveSession(sessionId: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/archive`),
        { method: "POST", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async unarchiveSession(sessionId: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/archive`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async uploadAttachments(
      sessionId: string,
      files: File[]
    ): Promise<AttachmentRecord[]> {
      const form = new FormData();
      for (const file of files) {
        form.append("files", file);
      }
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/attachments`),
        {
          method: "POST",
          headers: authHeaders(apiKey),
          body: form
        }
      );
      const payload = await handleJson<{ attachments: AttachmentRecord[] }>(
        response
      );
      return payload.attachments;
    },

    async createShare(sessionId: string): Promise<ShareRecord> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/share`),
        { method: "POST", headers: headers(apiKey) }
      );
      return handleJson<ShareRecord>(response);
    },

    async exportSession(sessionId: string): Promise<SessionExport> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/export`),
        { headers: headers(apiKey) }
      );
      return handleJson<SessionExport>(response);
    },

    async resolveShare(token: string): Promise<SessionExport> {
      const response = await fetch(withBase(baseUrl, `/api/share/${token}`), {
        headers: headers(apiKey)
      });
      return handleJson<SessionExport>(response);
    },

    async sendMessage(sessionId: string, text: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/message`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ text })
        }
      );
      await handleJson(response);
    },

    async interruptStep(sessionId: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/interrupt`),
        {
          method: "POST",
          headers: headers(apiKey)
        }
      );
      await handleJson(response);
    },

    async listTools(): Promise<ToolSummary[]> {
      const response = await fetch(withBase(baseUrl, "/api/tools"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ tools: ToolSummary[] }>(response);
      return payload.tools;
    },

    async listSkills(): Promise<SkillSummary[]> {
      const response = await fetch(withBase(baseUrl, "/api/skills"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ skills: SkillSummary[] }>(response);
      return payload.skills;
    },

    async listNotifications(): Promise<NotificationItem[]> {
      const response = await fetch(withBase(baseUrl, "/api/notifications"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ notifications: NotificationItem[] }>(
        response
      );
      return payload.notifications;
    },

    async dismissNotification(ids: string[]): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, "/api/notifications/dismiss"),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({
            ids
          })
        }
      );
      await handleJson(response);
    },

    async clearNotifications(clearAll = false): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, "/api/notifications/clear"),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({
            clear_all: clearAll
          })
        }
      );
      await handleJson(response);
    }
  };
}
