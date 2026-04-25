// Thin façade over `realClient` — exports each API function as a direct call.
// The runtime mock fallback was removed in Phase 4; tests now own their mocks
// via `vi.mock('../api/client', ...)` and fixtures live in
// `src/__tests__/fixtures/mockData.ts`.
import {
  AttachmentPayload,
  AttachmentRecord,
  EventRecord,
  NotificationItem,
  QueryMode,
  SessionContext,
  SessionExport,
  SessionSummary,
  SessionUsage,
  ShareRecord
} from '../types';
import { AgentSummary, MarketplacePlugin, ModelInfo, PluginSummary, ProjectSummary, SkillSummary, ToolSummary } from './contracts';
import { createRealClient } from './realClient';

// Runtime config injected by nginx (docker), falls back to Vite build-time env.
const _rc = (window as unknown as Record<string, unknown>).__TRUSS_CONFIG__ as
  Record<string, string> | undefined;

const USE_PROXY = parseBool(_rc?.VITE_API_USE_PROXY ?? import.meta.env.VITE_API_USE_PROXY);
export const API_BASE =
  USE_PROXY
    ? ''
    : _rc?.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE ?? '';
export const API_KEY = _rc?.VITE_API_KEY ?? import.meta.env.VITE_API_KEY ?? '';

const realClient = createRealClient({ baseUrl: API_BASE, apiKey: API_KEY });

function parseBool(value?: string): boolean {
  if (!value) {
    return false;
  }
  return ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase());
}

// ---------------------------------------------------------------------------
// Exported API functions — each one is a direct delegate to realClient.
// ---------------------------------------------------------------------------

export async function listSessions(includeArchived = false): Promise<SessionSummary[]> {
  return realClient.listSessions(includeArchived);
}

export async function createSession(context?: SessionContext): Promise<string> {
  return realClient.createSession(context);
}

export async function postQuery(
  sessionId: string,
  query: string,
  context?: SessionContext,
  mode?: QueryMode,
  attachments?: AttachmentPayload[]
): Promise<void> {
  return realClient.postQuery(sessionId, query, context, mode, attachments);
}

export async function fetchEvents(
  sessionId: string,
  after?: string
): Promise<{ events: EventRecord[]; running: boolean }> {
  return realClient.fetchEvents(sessionId, after);
}

export async function fetchUsage(sessionId: string): Promise<SessionUsage> {
  return realClient.fetchUsage(sessionId);
}

export async function archiveSession(sessionId: string): Promise<void> {
  return realClient.archiveSession(sessionId);
}

export async function unarchiveSession(sessionId: string): Promise<void> {
  return realClient.unarchiveSession(sessionId);
}

export async function updateSessionTitle(
  sessionId: string,
  title: string
): Promise<{ session_id: string; title: string }> {
  return realClient.updateSessionTitle(sessionId, title);
}

export async function regenerateTitle(
  sessionId: string
): Promise<{ session_id: string; title: string }> {
  return realClient.regenerateTitle(sessionId);
}

export async function uploadAttachments(
  sessionId: string,
  files: File[]
): Promise<AttachmentRecord[]> {
  return realClient.uploadAttachments(sessionId, files);
}

export async function createShare(sessionId: string): Promise<ShareRecord> {
  return realClient.createShare(sessionId);
}

export async function exportSession(sessionId: string): Promise<SessionExport> {
  return realClient.exportSession(sessionId);
}

export async function resolveShare(token: string): Promise<SessionExport> {
  return realClient.resolveShare(token);
}

export async function listTools(project?: string): Promise<ToolSummary[]> {
  return realClient.listTools(project);
}

export async function listModels(): Promise<ModelInfo> {
  return realClient.listModels();
}

export async function listProjects(): Promise<ProjectSummary[]> {
  return realClient.listProjects();
}

export async function listSkills(project?: string): Promise<SkillSummary[]> {
  return realClient.listSkills(project);
}

export async function listNotifications(): Promise<NotificationItem[]> {
  return realClient.listNotifications();
}

export async function dismissNotification(ids: string[]): Promise<void> {
  return realClient.dismissNotification(ids);
}

export async function clearNotifications(clearAll = false): Promise<void> {
  return realClient.clearNotifications(clearAll);
}

export async function sendMessage(sessionId: string, text: string): Promise<void> {
  return realClient.sendMessage(sessionId, text);
}

export async function interruptStep(sessionId: string): Promise<void> {
  return realClient.interruptStep(sessionId);
}

export async function approvePlan(sessionId: string, approved: boolean): Promise<void> {
  return realClient.approvePlan(sessionId, approved);
}

export async function recoverSession(
  sessionId: string,
  action: "retry" | "continue",
  fromTs?: string,
  editedText?: string,
  model?: string
): Promise<void> {
  return realClient.recoverSession(sessionId, action, fromTs, editedText, model);
}

export async function forkSession(
  sessionId: string,
  opts?: { fromTs?: string; model?: string; compact?: boolean; tag?: string }
): Promise<{ session_id: string; forked_from: string; forked_at: string | null }> {
  return realClient.forkSession(sessionId, opts);
}

export async function fetchPlanMarkdown(sessionId: string): Promise<string> {
  return realClient.fetchPlanMarkdown(sessionId);
}

export async function listAgents(sessionId: string): Promise<{
  agents: AgentSummary[];
  running: boolean;
  total_steps: number;
  total_input_tokens: number;
  total_output_tokens: number;
}> {
  return realClient.listAgents(sessionId);
}

export async function getConfigSchema(): Promise<Record<string, unknown>> {
  return realClient.getConfigSchema();
}

export async function getConfig(): Promise<Record<string, unknown>> {
  return realClient.getConfig();
}

export async function patchConfig(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
  return realClient.patchConfig(patch);
}

export async function listPlugins(): Promise<PluginSummary[]> {
  return realClient.listPlugins();
}

export async function listMarketplacePlugins(): Promise<MarketplacePlugin[]> {
  return realClient.listMarketplacePlugins();
}

export async function installPlugin(name: string, marketplace: string): Promise<void> {
  return realClient.installPlugin(name, marketplace);
}

export async function uninstallPlugin(name: string): Promise<void> {
  return realClient.uninstallPlugin(name);
}

export async function createVirtualProject(name: string, description: string, path?: string) {
  return realClient.createVirtualProject(name, description, path);
}

export async function updateVirtualProject(id: string, data: { name?: string; description?: string }) {
  return realClient.updateVirtualProject(id, data);
}

export async function deleteVirtualProject(id: string): Promise<void> {
  return realClient.deleteVirtualProject(id);
}

export async function fetchGitDiff(
  sessionId: string,
  scope: "uncommitted" | "branch"
): Promise<{ git_repo: boolean; reason?: string; diff?: string }> {
  const res = await fetch(
    `${API_BASE}/api/sessions/${sessionId}/git-diff?scope=${scope}`,
    { headers: { "X-Api-Key": API_KEY } }
  );
  if (!res.ok) throw new Error(`git-diff ${res.status}`);
  return res.json();
}

export async function fetchCommands(): Promise<import("../types").CommandSpec[]> {
  return realClient.fetchCommands();
}

export async function executeCommand(
  sessionId: string,
  name: string,
  args: string[]
): Promise<import("../types").CommandResult> {
  return realClient.executeCommand(sessionId, name, args);
}

export type { AgentSummary, MarketplacePlugin, PluginSummary, ProjectSummary, SkillSummary, ToolSummary };
