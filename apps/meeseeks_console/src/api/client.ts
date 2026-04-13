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
} from '../types';
import { AgentSummary, ApiClient, ApiMode, MarketplacePlugin, ModelInfo, PluginSummary, ProjectSummary, SkillSummary, ToolSummary } from './contracts';
import { createRealClient } from './realClient';
import {
  mockListSessions,
  mockCreateSession,
  mockPostQuery,
  mockFetchEvents,
  mockArchiveSession,
  mockUnarchiveSession,
  mockUpdateSessionTitle,
  mockUploadAttachments,
  mockCreateShare,
  mockExportSession,
  mockResolveShare,
  mockListTools,
  mockListSkills,
  mockListNotifications,
  mockDismissNotification,
  mockClearNotifications
} from '../mocks/mockData';

// Runtime config injected by nginx (docker), falls back to Vite build-time env.
const _rc = (window as unknown as Record<string, unknown>).__MEESEEKS_CONFIG__ as
  Record<string, string> | undefined;

const USE_PROXY = parseBool(_rc?.VITE_API_USE_PROXY ?? import.meta.env.VITE_API_USE_PROXY);
export const API_BASE =
USE_PROXY ?
'' :
_rc?.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE ?? '';
export const API_KEY = _rc?.VITE_API_KEY ?? import.meta.env.VITE_API_KEY ?? '';
const API_MODE = resolveApiMode(_rc?.VITE_API_MODE ?? import.meta.env.VITE_API_MODE);

const realClient = createRealClient({ baseUrl: API_BASE, apiKey: API_KEY });
const mockClient: ApiClient = {
  listSessions: mockListSessions,
  createSession: mockCreateSession,
  postQuery: mockPostQuery,
  fetchEvents: mockFetchEvents,
  archiveSession: mockArchiveSession,
  unarchiveSession: mockUnarchiveSession,
  updateSessionTitle: mockUpdateSessionTitle,
  regenerateTitle: async (sessionId: string) => ({ session_id: sessionId, title: "AI Generated Title" }),
  uploadAttachments: mockUploadAttachments,
  createShare: mockCreateShare,
  exportSession: mockExportSession,
  resolveShare: mockResolveShare,
  sendMessage: async () => { /* no-op mock */ },
  interruptStep: async () => { /* no-op mock */ },
  approvePlan: async () => { /* no-op mock */ },
  recoverSession: async (_s?: string, _a?: string, _t?: string, _e?: string, _m?: string) => { /* no-op mock */ },
  forkSession: async (sessionId: string) => ({
    session_id: `mock-fork-${sessionId}`,
    forked_from: sessionId,
    forked_at: null,
  }),
  fetchPlanMarkdown: async () => "",
  streamEvents: () => () => { /* no-op mock */ },
  listTools: (_project?: string) => mockListTools(),
  listSkills: (_project?: string) => mockListSkills(),
  listModels: async () => ({ models: [], default: 'unknown' }),
  listProjects: async () => [],
  listNotifications: mockListNotifications,
  dismissNotification: mockDismissNotification,
  clearNotifications: mockClearNotifications,
  listAgents: async () => ({ agents: [], running: false, total_steps: 0 }),
  getConfigSchema: async () => ({ type: "object", properties: {} }),
  getConfig: async () => ({}),
  patchConfig: async () => ({}),
  listPlugins: async () => [],
  listMarketplacePlugins: async () => [],
  installPlugin: async () => { /* no-op mock */ },
  uninstallPlugin: async () => { /* no-op mock */ },
  createVirtualProject: async (name: string, description: string, path?: string) => ({
    project_id: "mock-id",
    name,
    description,
    path: path || "/tmp/mock-project",
    path_source: path ? "provided" : "auto",
    folder_created: true,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }),
  updateVirtualProject: async (id: string, data: { name?: string; description?: string }) => ({
    project_id: id,
    name: data.name || "Mock Project",
    description: data.description || "",
    path: "/tmp/mock-project",
    path_source: "auto",
    folder_created: true,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
  }),
  deleteVirtualProject: async () => { /* no-op mock */ },
};

// When true in auto mode, skip real fetch and use mocks directly.
// Flips to true after the first network/API failure.
let fallbackToMock = false;

// ---------------------------------------------------------------------------
// Simple in-memory TTL cache
// ---------------------------------------------------------------------------

type CacheEntry<T> = { data: T; ts: number };
const _cache = new Map<string, CacheEntry<unknown>>();
const CACHE_TTL = 60_000;

function getCached<T>(key: string): T | undefined {
  const e = _cache.get(key);
  if (!e || Date.now() - e.ts > CACHE_TTL) {
    if (e) _cache.delete(key);
    return undefined;
  }
  return e.data as T;
}

/** Return cached data regardless of TTL (stale-while-revalidate reads). */
export function peekCache<T>(key: string): T | undefined {
  const e = _cache.get(key);
  return e ? (e.data as T) : undefined;
}

function setCache<T>(key: string, data: T): void {
  _cache.set(key, { data, ts: Date.now() });
}

export function invalidateCache(prefix?: string): void {
  if (!prefix) {
    _cache.clear();
    return;
  }
  for (const k of _cache.keys()) {
    if (k.startsWith(prefix)) _cache.delete(k);
  }
}

function parseBool(value?: string): boolean {
  if (!value) {
    return false;
  }
  return ['1', 'true', 'yes', 'on'].includes(value.trim().toLowerCase());
}

function resolveApiMode(raw?: string): ApiMode {
  const normalized = String(raw || '')
  .trim()
  .toLowerCase();
  if (normalized === 'mock') {
    return 'mock';
  }
  if (normalized === 'live' || normalized === 'real') {
    return 'live';
  }
  return 'auto';
}

function shouldUseMock(): boolean {
  if (API_MODE === 'mock') {
    return true;
  }
  if (API_MODE === 'live') {
    return false;
  }
  return fallbackToMock;
}

async function withFallback<T>(
  realFn: () => Promise<T>,
  mockFn: () => Promise<T>
): Promise<T> {
  if (shouldUseMock()) {
    return mockFn();
  }
  if (API_MODE === 'live') {
    return realFn();
  }
  try {
    return await realFn();
  } catch {
    fallbackToMock = true;
    return mockFn();
  }
}

// ---------------------------------------------------------------------------
// Exported API functions — try real backend, fall back to mock when allowed
// ---------------------------------------------------------------------------

export async function listSessions(
includeArchived = false)
: Promise<SessionSummary[]> {
  return withFallback(
    () => realClient.listSessions(includeArchived),
    () => mockClient.listSessions(includeArchived)
  );
}

export async function createSession(context?: SessionContext): Promise<string> {
  return withFallback(
    () => realClient.createSession(context),
    () => mockClient.createSession(context)
  );
}

export async function postQuery(
sessionId: string,
query: string,
context?: SessionContext,
mode?: QueryMode,
attachments?: AttachmentPayload[])
: Promise<void> {
  return withFallback(
    () => realClient.postQuery(sessionId, query, context, mode, attachments),
    () => mockClient.postQuery(sessionId, query, context, mode, attachments)
  );
}

export async function fetchEvents(
sessionId: string,
after?: string)
: Promise<{events: EventRecord[];running: boolean;}> {
  return withFallback(
    () => realClient.fetchEvents(sessionId, after),
    () => mockClient.fetchEvents(sessionId, after)
  );
}

export async function archiveSession(sessionId: string): Promise<void> {
  return withFallback(
    () => realClient.archiveSession(sessionId),
    () => mockClient.archiveSession(sessionId)
  );
}

export async function unarchiveSession(sessionId: string): Promise<void> {
  return withFallback(
    () => realClient.unarchiveSession(sessionId),
    () => mockClient.unarchiveSession(sessionId)
  );
}

export async function updateSessionTitle(
sessionId: string,
title: string)
: Promise<{session_id: string;title: string;}> {
  const result = await withFallback(
    () => realClient.updateSessionTitle(sessionId, title),
    () => mockClient.updateSessionTitle(sessionId, title)
  );
  invalidateCache('sessions');
  return result;
}

export async function regenerateTitle(
  sessionId: string
): Promise<{ session_id: string; title: string }> {
  const result = await withFallback(
    () => realClient.regenerateTitle(sessionId),
    () => mockClient.regenerateTitle(sessionId)
  );
  invalidateCache('sessions');
  return result;
}

export async function uploadAttachments(
sessionId: string,
files: File[])
: Promise<AttachmentRecord[]> {
  return withFallback(
    () => realClient.uploadAttachments(sessionId, files),
    () => mockClient.uploadAttachments(sessionId, files)
  );
}

export async function createShare(sessionId: string): Promise<ShareRecord> {
  return withFallback(
    () => realClient.createShare(sessionId),
    () => mockClient.createShare(sessionId)
  );
}

export async function exportSession(sessionId: string): Promise<SessionExport> {
  return withFallback(
    () => realClient.exportSession(sessionId),
    () => mockClient.exportSession(sessionId)
  );
}

export async function resolveShare(token: string): Promise<SessionExport> {
  return withFallback(
    () => realClient.resolveShare(token),
    () => mockClient.resolveShare(token)
  );
}

export async function listTools(project?: string): Promise<ToolSummary[]> {
  const key = `tools:${project ?? ''}`;
  const hit = getCached<ToolSummary[]>(key);
  if (hit) return hit;
  const result = await withFallback(
    () => realClient.listTools(project),
    () => mockClient.listTools(project)
  );
  setCache(key, result);
  return result;
}

export async function listModels(): Promise<ModelInfo> {
  const key = 'models';
  const hit = getCached<ModelInfo>(key);
  if (hit) return hit;
  const result = await withFallback(
    () => realClient.listModels(),
    () => mockClient.listModels()
  );
  setCache(key, result);
  return result;
}

export async function listProjects(): Promise<ProjectSummary[]> {
  const key = 'projects';
  const hit = getCached<ProjectSummary[]>(key);
  if (hit) return hit;
  const result = await withFallback(
    () => realClient.listProjects(),
    () => mockClient.listProjects()
  );
  setCache(key, result);
  return result;
}

export async function listSkills(project?: string): Promise<SkillSummary[]> {
  const key = `skills:${project ?? ''}`;
  const hit = getCached<SkillSummary[]>(key);
  if (hit) return hit;
  const result = await withFallback(
    () => realClient.listSkills(project),
    () => mockClient.listSkills(project)
  );
  setCache(key, result);
  return result;
}

export async function listNotifications(): Promise<NotificationItem[]> {
  return withFallback(
    () => realClient.listNotifications(),
    () => mockClient.listNotifications()
  );
}

export async function dismissNotification(ids: string[]): Promise<void> {
  return withFallback(
    () => realClient.dismissNotification(ids),
    () => mockClient.dismissNotification(ids)
  );
}

export async function clearNotifications(clearAll = false): Promise<void> {
  return withFallback(
    () => realClient.clearNotifications(clearAll),
    () => mockClient.clearNotifications(clearAll)
  );
}

export async function sendMessage(sessionId: string, text: string): Promise<void> {
  return withFallback(
    () => realClient.sendMessage(sessionId, text),
    () => mockClient.sendMessage(sessionId, text)
  );
}

export async function interruptStep(sessionId: string): Promise<void> {
  return withFallback(
    () => realClient.interruptStep(sessionId),
    () => mockClient.interruptStep(sessionId)
  );
}

export async function approvePlan(sessionId: string, approved: boolean): Promise<void> {
  return withFallback(
    () => realClient.approvePlan(sessionId, approved),
    () => mockClient.approvePlan(sessionId, approved)
  );
}

export async function recoverSession(
  sessionId: string,
  action: "retry" | "continue",
  fromTs?: string,
  editedText?: string,
  model?: string
): Promise<void> {
  return withFallback(
    () => realClient.recoverSession(sessionId, action, fromTs, editedText, model),
    () => mockClient.recoverSession(sessionId, action, fromTs, editedText, model)
  );
}

export async function forkSession(
  sessionId: string,
  opts?: { fromTs?: string; model?: string; compact?: boolean; tag?: string }
): Promise<{ session_id: string; forked_from: string; forked_at: string | null }> {
  const result = await withFallback(
    () => realClient.forkSession(sessionId, opts),
    () => mockClient.forkSession(sessionId, opts)
  );
  invalidateCache('sessions');
  return result;
}

export async function fetchPlanMarkdown(sessionId: string): Promise<string> {
  return withFallback(
    () => realClient.fetchPlanMarkdown(sessionId),
    () => mockClient.fetchPlanMarkdown(sessionId)
  );
}

export async function listAgents(sessionId: string): Promise<{
  agents: AgentSummary[];
  running: boolean;
  total_steps: number;
}> {
  return withFallback(
    () => realClient.listAgents(sessionId),
    () => mockClient.listAgents(sessionId)
  );
}

export async function getConfigSchema(): Promise<Record<string, unknown>> {
  const key = 'config:schema';
  const hit = getCached<Record<string, unknown>>(key);
  if (hit) return hit;
  const result = await withFallback(
    () => realClient.getConfigSchema(),
    () => mockClient.getConfigSchema()
  );
  setCache(key, result);
  return result;
}

let _configPromise: Promise<Record<string, unknown>> | null = null;

/**
 * Fetch the runtime config. De-duplicated across callers: App.tsx (Langfuse),
 * useWebIdeEnabled, and useConfig all share one in-flight promise per page
 * load. ``patchConfig`` invalidates so the next read refetches.
 */
export async function getConfig(): Promise<Record<string, unknown>> {
  if (_configPromise === null) {
    _configPromise = withFallback(
      () => realClient.getConfig(),
      () => mockClient.getConfig()
    ).catch((err) => {
      _configPromise = null; // Let the next caller retry.
      throw err;
    });
  }
  return _configPromise;
}

export async function patchConfig(patch: Record<string, unknown>): Promise<Record<string, unknown>> {
  invalidateCache('config:');
  _configPromise = null;
  return withFallback(
    () => realClient.patchConfig(patch),
    () => mockClient.patchConfig(patch)
  );
}

export async function listPlugins(): Promise<PluginSummary[]> {
  return withFallback(
    () => realClient.listPlugins(),
    () => mockClient.listPlugins()
  );
}

export async function listMarketplacePlugins(): Promise<MarketplacePlugin[]> {
  return withFallback(
    () => realClient.listMarketplacePlugins(),
    () => mockClient.listMarketplacePlugins()
  );
}

export async function installPlugin(name: string, marketplace: string): Promise<void> {
  return withFallback(
    () => realClient.installPlugin(name, marketplace),
    () => mockClient.installPlugin(name, marketplace)
  );
}

export async function uninstallPlugin(name: string): Promise<void> {
  return withFallback(
    () => realClient.uninstallPlugin(name),
    () => mockClient.uninstallPlugin(name)
  );
}

export async function createVirtualProject(name: string, description: string, path?: string) {
  return withFallback(
    () => realClient.createVirtualProject(name, description, path),
    () => mockClient.createVirtualProject(name, description, path)
  );
}

export async function updateVirtualProject(id: string, data: { name?: string; description?: string }) {
  return withFallback(
    () => realClient.updateVirtualProject(id, data),
    () => mockClient.updateVirtualProject(id, data)
  );
}

export async function deleteVirtualProject(id: string): Promise<void> {
  return withFallback(
    () => realClient.deleteVirtualProject(id),
    () => mockClient.deleteVirtualProject(id)
  );
}

export async function fetchGitDiff(
  sessionId: string,
  scope: "uncommitted" | "branch"
): Promise<{ git_repo: boolean; reason?: string; diff?: string }> {
  return withFallback(
    async () => {
      const res = await fetch(
        `${API_BASE}/api/sessions/${sessionId}/git-diff?scope=${scope}`,
        { headers: { "X-Api-Key": API_KEY } }
      );
      if (!res.ok) throw new Error(`git-diff ${res.status}`);
      return res.json();
    },
    async () => ({ git_repo: false })
  );
}

export type { AgentSummary, MarketplacePlugin, PluginSummary, ProjectSummary, SkillSummary, ToolSummary };
