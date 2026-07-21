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
  SessionSpecResponse,
  SessionSummary,
  SessionUsage,
  ShareRecord
} from "../types";
import { QuestionAnswerItemPayload } from "../types";
import { AgentSummary, AnswerQuestionResult, ApiClient, ApiConfig, ApiKeyCreated, ApiKeyRevoked, ApiKeySummary, ConfigState, CreateWorktreeInput, ForkResponse, MarketplacePlugin, ModelInfo, PluginSummary, ProjectBranches, ProjectSummary, RecoverResponse, SkillSummary, ToolSummary, VirtualProject, WorktreeSummary } from "./contracts";
import { apiFetch, withBase, authHeaders, readError, readJson as handleJson } from "./httpBase";

// Capability IDs can be overridden at build time. Each must match the id in a
// plugin manifest's requires-capabilities (hardcoded server-side; there is no
// server config key for them):
//   - `stlite`   → the widget-builder plugin (chat `widget_ready` cards).
//   - `apps`     → the app-builder plugin (Mewbo Apps sub-product). Advertised
//     on every session-driving request so a ROOT session can delegate to the
//     app-builder AgentDef (two-surface gating: catalogs AND build_for).
//   - `ask_user` → core's ASK_USER_CAPABILITY. The console renders the
//     ask-user-question card and POSTs the answer, so it advertises this on
//     the ordinary chat/query path. Headless product drives (wiki/search) use
//     their own client and never send this — they must not bind the
//     block-until-answered tool.
// The header is a comma-separated list (backend.py splits on "," and strips),
// so all three grants ride every session the console opens.
const WIDGET_CAPABILITY_ID =
  (import.meta.env.VITE_WIDGET_CAPABILITY_ID as string | undefined) || "stlite";
const APPS_CAPABILITY_ID =
  (import.meta.env.VITE_APPS_CAPABILITY_ID as string | undefined) || "apps";
const ASK_USER_CAPABILITY_ID = "ask_user";
const CLIENT_CAPABILITIES = [
  WIDGET_CAPABILITY_ID,
  APPS_CAPABILITY_ID,
  ASK_USER_CAPABILITY_ID
].join(",");

function headers(apiKey?: string): HeadersInit {
  return {
    ...authHeaders(apiKey),
    "Content-Type": "application/json",
    "X-Mewbo-Capabilities": CLIENT_CAPABILITIES,
    "X-Mewbo-Surface": "console",
  };
}

export function createRealClient(config: ApiConfig): ApiClient {
  const baseUrl = config.baseUrl || "";
  const apiKey = config.apiKey || "";

  return {
    async listSessions(includeArchived = false): Promise<SessionSummary[]> {
      const params = includeArchived ? "?include_archived=1" : "";
      const response = await apiFetch(withBase(baseUrl, `/api/sessions${params}`), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ sessions: SessionSummary[] }>(response);
      return payload.sessions;
    },

    async createSession(context?: SessionContext): Promise<string> {
      const response = await apiFetch(withBase(baseUrl, "/api/sessions"), {
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
      const response = await apiFetch(
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
    ): Promise<{
      events: EventRecord[];
      running: boolean;
      status?: string;
      done_reason?: string;
      terminated?: boolean;
      recoverable?: boolean;
    }> {
      const params = after ? `?after=${encodeURIComponent(after)}` : "";
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/events${params}`),
        { headers: headers(apiKey) }
      );
      const payload = await handleJson<{
        events: EventRecord[];
        running: boolean;
        status?: string;
        done_reason?: string;
        terminated?: boolean;
        recoverable?: boolean;
      }>(response);
      return payload;
    },

    async fetchUsage(sessionId: string): Promise<SessionUsage> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/usage`),
        { headers: headers(apiKey) }
      );
      return handleJson<SessionUsage>(response);
    },

    async getSessionSpec(sessionId: string): Promise<SessionSpecResponse> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/spec`),
        { headers: headers(apiKey) }
      );
      return handleJson<SessionSpecResponse>(response);
    },

    async archiveSession(sessionId: string): Promise<void> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/archive`),
        { method: "POST", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async unarchiveSession(sessionId: string): Promise<void> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/archive`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async updateSessionTitle(
      sessionId: string,
      title: string
    ): Promise<{ session_id: string; title: string }> {
      const response = await apiFetch(
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
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/title`),
        { method: "POST", headers: headers(apiKey) }
      );
      return handleJson<{ session_id: string; title: string }>(response);
    },

    async uploadAttachments(
      sessionId: string,
      files: File[],
      model?: string | null
    ): Promise<AttachmentRecord[]> {
      const form = new FormData();
      for (const file of files) {
        form.append("files", file);
      }
      // Backend uses ``model`` to reject images on non-vision models with
      // a 400 instead of silently dropping them at inference time.
      if (model) {
        form.append("model", model);
      }
      const response = await apiFetch(
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
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/share`),
        { method: "POST", headers: headers(apiKey) }
      );
      return handleJson<ShareRecord>(response);
    },

    async exportSession(sessionId: string): Promise<SessionExport> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/export`),
        { headers: headers(apiKey) }
      );
      return handleJson<SessionExport>(response);
    },

    async resolveShare(token: string): Promise<SessionExport> {
      const response = await apiFetch(withBase(baseUrl, `/api/share/${token}`), {
        headers: headers(apiKey)
      });
      return handleJson<SessionExport>(response);
    },

    async sendMessage(sessionId: string, text: string): Promise<void> {
      const response = await apiFetch(
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
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/interrupt`),
        {
          method: "POST",
          headers: headers(apiKey)
        }
      );
      await handleJson(response);
    },

    async approvePlan(sessionId: string, approved: boolean): Promise<void> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/plan/approve`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ approved })
        }
      );
      await handleJson(response);
    },

    async answerQuestion(
      sessionId: string,
      callId: string,
      body: { call_token: string; answers: QuestionAnswerItemPayload[] }
    ): Promise<AnswerQuestionResult> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/questions/${callId}/answer`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify(body)
        }
      );
      if (response.ok) return { ok: true };
      // Classify the status so the card settles silently when the question was
      // resolved elsewhere (404/409/410) vs. surfacing a correctable message.
      const kind =
        response.status === 404 || response.status === 409
          ? "superseded"
          : response.status === 422
            ? "invalid"
            : response.status === 403
              ? "forbidden"
              : response.status === 410
                ? "terminated"
                : "error";
      const message = (await readError(response)).message;
      return { ok: false, kind, message };
    },

    async recoverSession(
      sessionId: string,
      action: "retry" | "continue",
      fromTs?: string,
      editedText?: string,
      model?: string
    ): Promise<RecoverResponse> {
      const body: Record<string, unknown> = { action };
      if (fromTs) body.from_ts = fromTs;
      if (editedText) body.edited_text = editedText;
      if (model) body.model = model;
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/recover`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify(body)
        }
      );
      return handleJson<RecoverResponse>(response);
    },

    async forkSession(
      sessionId: string,
      opts?: { fromTs?: string; model?: string; compact?: boolean; tag?: string }
    ): Promise<ForkResponse> {
      const body: Record<string, unknown> = {};
      if (opts?.fromTs) body.from_ts = opts.fromTs;
      if (opts?.model) body.model = opts.model;
      if (opts?.compact) body.compact = true;
      if (opts?.tag) body.tag = opts.tag;
      const response = await apiFetch(
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
      const response = await apiFetch(
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
      const response = await apiFetch(withBase(baseUrl, `/api/tools${params}`), {
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
      const response = await apiFetch(withBase(baseUrl, "/api/models"), {
        headers: headers(apiKey)
      });
      return handleJson<ModelInfo>(response);
    },

    async listProjects(): Promise<ProjectSummary[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/projects"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ projects: ProjectSummary[] }>(response);
      return payload.projects;
    },

    async listSkills(project?: string): Promise<SkillSummary[]> {
      const params = project ? `?project=${encodeURIComponent(project)}` : "";
      const response = await apiFetch(withBase(baseUrl, `/api/skills${params}`), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ skills: SkillSummary[] }>(response);
      return payload.skills;
    },

    async listNotifications(): Promise<NotificationItem[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/notifications"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ notifications: NotificationItem[] }>(
        response
      );
      return payload.notifications;
    },

    async dismissNotification(ids: string[]): Promise<void> {
      const response = await apiFetch(
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
      const response = await apiFetch(
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
      const response = await apiFetch(
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
      const response = await apiFetch(withBase(baseUrl, "/api/config/schema"), {
        headers: headers(apiKey)
      });
      return handleJson<Record<string, unknown>>(response);
    },

    async getConfig(): Promise<ConfigState> {
      const response = await apiFetch(withBase(baseUrl, "/api/config"), {
        headers: headers(apiKey)
      });
      const data = await handleJson<Partial<ConfigState>>(response);
      return { config: data.config ?? {}, secrets: data.secrets ?? {} };
    },

    async patchConfig(patch: Record<string, unknown>): Promise<ConfigState> {
      const response = await apiFetch(withBase(baseUrl, "/api/config"), {
        method: "PATCH",
        headers: headers(apiKey),
        body: JSON.stringify(patch)
      });
      const data = await handleJson<Partial<ConfigState>>(response);
      return { config: data.config ?? {}, secrets: data.secrets ?? {} };
    },

    async listPlugins(): Promise<PluginSummary[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/plugins"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ plugins: PluginSummary[] }>(response);
      return payload.plugins || [];
    },

    async listMarketplacePlugins(): Promise<MarketplacePlugin[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/plugins/marketplace"), {
        headers: headers(apiKey)
      });
      const payload = await handleJson<{ plugins: MarketplacePlugin[] }>(response);
      return payload.plugins || [];
    },

    async installPlugin(name: string, marketplace: string): Promise<void> {
      const response = await apiFetch(withBase(baseUrl, "/api/plugins/marketplace"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ name, marketplace })
      });
      await handleJson(response);
    },

    async uninstallPlugin(name: string): Promise<void> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/plugins/${encodeURIComponent(name)}`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async createVirtualProject(name: string, description: string, path?: string): Promise<VirtualProject> {
      const response = await apiFetch(withBase(baseUrl, "/api/v_projects"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ name, description, path })
      });
      return handleJson<VirtualProject>(response);
    },

    async updateVirtualProject(id: string, data: Partial<Pick<VirtualProject, "name" | "description">>): Promise<VirtualProject> {
      const response = await apiFetch(
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
      const response = await apiFetch(
        withBase(baseUrl, `/api/v_projects/${encodeURIComponent(id)}`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async listProjectBranches(projectId: string): Promise<ProjectBranches> {
      const response = await apiFetch(
        withBase(
          baseUrl,
          `/api/v_projects/${encodeURIComponent(projectId)}/branches`
        ),
        { headers: headers(apiKey) }
      );
      return handleJson<ProjectBranches>(response);
    },

    async listWorktrees(projectId: string): Promise<WorktreeSummary[]> {
      const response = await apiFetch(
        withBase(
          baseUrl,
          `/api/v_projects/${encodeURIComponent(projectId)}/worktrees`
        ),
        { headers: headers(apiKey) }
      );
      const payload = await handleJson<{ worktrees: WorktreeSummary[] }>(response);
      return payload.worktrees ?? [];
    },

    async createWorktree(
      projectId: string,
      input: CreateWorktreeInput,
    ): Promise<WorktreeSummary> {
      // ``base`` is optional. When set, the backend creates a fresh branch
      // from <base> via ``git worktree add -b``; when omitted, ``branch``
      // must already exist in the repo.
      const body: Record<string, unknown> = { branch: input.branch };
      if (input.base) body.base = input.base;
      const response = await apiFetch(
        withBase(
          baseUrl,
          `/api/v_projects/${encodeURIComponent(projectId)}/worktrees`
        ),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify(body)
        }
      );
      return handleJson<WorktreeSummary>(response);
    },

    async deleteWorktree(projectId: string, worktreeId: string, force = false): Promise<void> {
      const qs = force ? "?force=true" : "";
      const response = await apiFetch(
        withBase(
          baseUrl,
          `/api/v_projects/${encodeURIComponent(projectId)}/worktrees/${encodeURIComponent(
            worktreeId
          )}${qs}`
        ),
        { method: "DELETE", headers: headers(apiKey) }
      );
      await handleJson(response);
    },

    async fetchCommands(): Promise<CommandSpec[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/commands"), {
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
      const response = await apiFetch(
        withBase(baseUrl, `/api/sessions/${sessionId}/command`),
        {
          method: "POST",
          headers: headers(apiKey),
          body: JSON.stringify({ name, args }),
        },
      );
      return handleJson<CommandResult>(response);
    },

    async listApiKeys(): Promise<ApiKeySummary[]> {
      const response = await apiFetch(withBase(baseUrl, "/api/keys"), {
        headers: headers(apiKey),
      });
      const payload = await handleJson<{ keys: ApiKeySummary[] }>(response);
      return payload.keys ?? [];
    },

    async createApiKey(label: string): Promise<ApiKeyCreated> {
      const response = await apiFetch(withBase(baseUrl, "/api/keys"), {
        method: "POST",
        headers: headers(apiKey),
        body: JSON.stringify({ label }),
      });
      return handleJson<ApiKeyCreated>(response);
    },

    async revokeApiKey(id: string): Promise<ApiKeyRevoked> {
      const response = await apiFetch(
        withBase(baseUrl, `/api/keys/${encodeURIComponent(id)}`),
        { method: "DELETE", headers: headers(apiKey) }
      );
      return handleJson<ApiKeyRevoked>(response);
    },
  };
}
