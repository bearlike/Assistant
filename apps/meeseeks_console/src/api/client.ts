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
import { ApiClient, ApiMode, ProjectSummary, SkillSummary, ToolSummary } from './contracts';
import { createRealClient } from './realClient';
import {
  mockListSessions,
  mockCreateSession,
  mockPostQuery,
  mockFetchEvents,
  mockArchiveSession,
  mockUnarchiveSession,
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
const API_BASE =
USE_PROXY ?
'' :
_rc?.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE_URL ?? import.meta.env.VITE_API_BASE ?? '';
const API_KEY = _rc?.VITE_API_KEY ?? import.meta.env.VITE_API_KEY ?? '';
const API_MODE = resolveApiMode(_rc?.VITE_API_MODE ?? import.meta.env.VITE_API_MODE);

const realClient = createRealClient({ baseUrl: API_BASE, apiKey: API_KEY });
const mockClient: ApiClient = {
  listSessions: mockListSessions,
  createSession: mockCreateSession,
  postQuery: mockPostQuery,
  fetchEvents: mockFetchEvents,
  archiveSession: mockArchiveSession,
  unarchiveSession: mockUnarchiveSession,
  uploadAttachments: mockUploadAttachments,
  createShare: mockCreateShare,
  exportSession: mockExportSession,
  resolveShare: mockResolveShare,
  sendMessage: async () => { /* no-op mock */ },
  interruptStep: async () => { /* no-op mock */ },
  streamEvents: () => () => { /* no-op mock */ },
  listTools: mockListTools,
  listSkills: mockListSkills,
  listProjects: async () => [],
  listNotifications: mockListNotifications,
  dismissNotification: mockDismissNotification,
  clearNotifications: mockClearNotifications
};

// When true in auto mode, skip real fetch and use mocks directly.
// Flips to true after the first network/API failure.
let fallbackToMock = false;

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

export async function listTools(): Promise<ToolSummary[]> {
  return withFallback(
    () => realClient.listTools(),
    () => mockClient.listTools()
  );
}

export async function listProjects(): Promise<ProjectSummary[]> {
  return withFallback(
    () => realClient.listProjects(),
    () => mockClient.listProjects()
  );
}

export async function listSkills(): Promise<SkillSummary[]> {
  return withFallback(
    () => realClient.listSkills(),
    () => mockClient.listSkills()
  );
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

export type { ProjectSummary, SkillSummary, ToolSummary };
