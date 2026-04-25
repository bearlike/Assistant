import {
  AttachmentPayload,
  AttachmentRecord,
  CommandResult,
  CommandSpec,
  EventRecord,
  NotificationItem,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  SessionUsage,
  ShareRecord
} from "../types";
import { AgentSummary, ApiClient, ApiConfig, MarketplacePlugin, ModelInfo, PluginSummary, ProjectSummary, SkillSummary, ToolSummary, VirtualProject } from "./contracts";

function withBase(baseUrl: string, path: string) {
  if (!baseUrl) {
    return path;
  }
  return `${baseUrl.replace(/\/$/, "")}${path}`;
}

// Capability ID can be overridden at build time via VITE_WIDGET_CAPABILITY_ID
// to stay in sync with server-side agent.widget_builder.capability_id.
const WIDGET_CAPABILITY_ID =
  (import.meta.env.VITE_WIDGET_CAPABILITY_ID as string | undefined) || "stlite";

function authHeaders(apiKey?: string): HeadersInit {
  return apiKey ? { "X-API-Key": apiKey } : {};
}

function headers(apiKey?: string): HeadersInit {
  return {
    ...authHeaders(apiKey),
    "Content-Type": "application/json",
    "X-Truss-Capabilities": WIDGET_CAPABILITY_ID,
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

    async fetchUsage(sessionId: string): Promise<SessionUsage> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/usage`),
        { headers: headers(apiKey) }
      );
      return handleJson<SessionUsage>(response);
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

    async updateSessionTitle(
      sessionId: string,
      title: string
    ): Promise<{ session_id: string; title: string }> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/title`),
        {
          method: "PATCH",
          headers: headers(apiKey),
          body: JSON.stringify({ title })
        }
      );
      return handleJson<{ session_id: string; title: string }>(response);
    },

    async regenerateTitle(
      sessionId: string
    ): Promise<{ session_id: string; title: string }> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/title`),
        { method: "POST", headers: headers(apiKey) }
      );
      return handleJson<{ session_id: string; title: string }>(response);
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

    async approvePlan(sessionId: string, approved: boolean): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/plan/approve`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ approved })
        }
      );
      await handleJson(response);
    },

    async recoverSession(
      sessionId: string,
      action: "retry" | "continue",
      fromTs?: string,
      editedText?: string,
      model?: string
    ): Promise<void> {
      const body: Record<string, unknown> = { action };
      if (fromTs) body.from_ts = fromTs;
      if (editedText) body.edited_text = editedText;
      if (model) body.model = model;
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/recover`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify(body)
        }
      );
      await handleJson(response);
    },

    async forkSession(
      sessionId: string,
      opts?: { fromTs?: string; model?: string; compact?: boolean; tag?: string }
    ): Promise<{ session_id: string; forked_from: string; forked_at: string | null }> {
      const body: Record<string, unknown> = {};
      if (opts?.fromTs) body.from_ts = opts.fromTs;
      if (opts?.model) body.model = opts.model;
      if (opts?.compact) body.compact = true;
      if (opts?.tag) body.tag = opts.tag;
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/fork`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify(body)
        }
      );
      return handleJson(response);
    },

    async fetchPlanMarkdown(sessionId: string): Promise<string> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/plan.md`),
        { headers: headers(apiKey) }
      );
      if (!response.ok) {
        const text = await response.text();
        throw new Error(text || `Failed to fetch plan (${response.status})`);
      }
      return response.text();
    },

    async listTools(project?: string): Promise<ToolSummary[]> {
      const params = project ? `?project=${encodeURIComponent(project)}` : "";
      const response = await fetch(withBase(baseUrl, `/api/tools${params}`), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ tools: ToolSummary[] }>(response);
      return payload.tools;
    },

    streamEvents(
      sessionId: string,
      onEvent: (event: EventRecord) => void,
      onEnd: () => void
    ): () => void {
      const params = new URLSearchParams();
      if (apiKey) params.set("api_key", apiKey);
      const url = withBase(baseUrl, `/api/sessions/${sessionId}/stream?${params}`);
      const source = new EventSource(url);
      source.onmessage = (e) => {
        try {
          const event = JSON.parse(e.data);
          if (event.type === "stream_end") {
            source.close();
            onEnd();
            return;
          }
          onEvent(event);
        } catch {
          // Ignore malformed frames
        }
      };
      source.onerror = () => {
        source.close();
        onEnd();
      };
      return () => source.close();
    },

    async listModels(): Promise<ModelInfo> {
      const response = await fetch(withBase(baseUrl, "/api/models"), {
        headers: headers(apiKey)
      });
      return handleJson<ModelInfo>(response);
    },

    async listProjects(): Promise<ProjectSummary[]> {
      const response = await fetch(withBase(baseUrl, "/api/projects"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ projects: ProjectSummary[] }>(response);
      return payload.projects;
    },

    async listSkills(project?: string): Promise<SkillSummary[]> {
      const params = project ? `?project=${encodeURIComponent(project)}` : "";
      const response = await fetch(withBase(baseUrl, `/api/skills${params}`), {
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
    },

    async listAgents(sessionId: string): Promise<{
      agents: AgentSummary[];
      running: boolean;
      total_steps: number;
      total_input_tokens: number;
      total_output_tokens: number;
    }> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/agents`),
        { headers: headers(apiKey) }
      );
      return handleJson<{
        agents: AgentSummary[];
        running: boolean;
        total_steps: number;
        total_input_tokens: number;
        total_output_tokens: number;
      }>(response);
    },

    async getConfigSchema(): Promise<Record<string, unknown>> {
      const response = await fetch(withBase(baseUrl, "/api/config/schema"), {
        headers: headers(apiKey)
      });
      return handleJson<Record<string, unknown>>(response);
    },

    async getConfig(): Promise<Record<string, unknown>> {
      const response = await fetch(withBase(baseUrl, "/api/config"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ config: Record<string, unknown> }>(response);
      return payload.config;
    },

    async patchConfig(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
      const response = await fetch(withBase(baseUrl, "/api/config"), {
        method: "PATCH",
        headers: headers(apiKey),
        body: JSON.stringify(patch)
      });
      const payload = await handleJson<{ config: Record<string, unknown> }>(response);
      return payload.config;
    },

    async listPlugins(): Promise<PluginSummary[]> {
      const response = await fetch(withBase(baseUrl, "/api/plugins"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ plugins: PluginSummary[] }>(response);
      return payload.plugins || [];
    },

    async listMarketplacePlugins(): Promise<MarketplacePlugin[]> {
      const response = await fetch(withBase(baseUrl, "/api/plugins/marketplace"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ plugins: MarketplacePlugin[] }>(response);
      return payload.plugins || [];
    },

    async installPlugin(name: string, marketplace: string): Promise<void> {
      const response = await fetch(withBase(baseUrl, "/api/plugins/marketplace"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ name, marketplace })
      });
      await handleJson(response);
    },

    async uninstallPlugin(name: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/plugins/${encodeURIComponent(name)}`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async createVirtualProject(name: string, description: string, path?: string): Promise<VirtualProject> {
      const response = await fetch(withBase(baseUrl, "/api/v_projects"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ name, description, path })
      });
      return handleJson<VirtualProject>(response);
    },

    async updateVirtualProject(id: string, data: Partial<Pick<VirtualProject, "name" | "description">>): Promise<VirtualProject> {
      const response = await fetch(
        withBase(baseUrl, `/api/v_projects/${encodeURIComponent(id)}`),
        {
          method: "PATCH",
          headers: headers(apiKey),
          body: JSON.stringify(data)
        }
      );
      return handleJson<VirtualProject>(response);
    },

    async deleteVirtualProject(id: string): Promise<void> {
      const response = await fetch(
        withBase(baseUrl, `/api/v_projects/${encodeURIComponent(id)}`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async fetchCommands(): Promise<CommandSpec[]> {
      const response = await fetch(withBase(baseUrl, "/api/commands"), {
        headers: headers(apiKey),
      });
      const payload = await handleJson<{ commands: CommandSpec[] }>(response);
      return payload.commands;
    },

    async executeCommand(
      sessionId: string,
      name: string,
      args: string[],
    ): Promise<CommandResult> {
      const response = await fetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/command`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ name, args }),
        },
      );
      return handleJson<CommandResult>(response);
    },
  };
}
