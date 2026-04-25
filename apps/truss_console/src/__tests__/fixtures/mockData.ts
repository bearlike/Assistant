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
} from '../../types';
import { SkillSummary, ToolSummary } from '../../api/contracts';

// ---------------------------------------------------------------------------
// In-memory state (persists for the browser session)
// ---------------------------------------------------------------------------

const NOW = new Date().toISOString();
const HOUR_AGO = new Date(Date.now() - 3600_000).toISOString();
const TWO_HOURS_AGO = new Date(Date.now() - 7200_000).toISOString();
const YESTERDAY = new Date(Date.now() - 86400_000).toISOString();
const THREE_DAYS_AGO = new Date(Date.now() - 259200_000).toISOString();
const TWO_WEEKS_AGO = new Date(Date.now() - 1209600_000).toISOString();
const ONE_MONTH_AGO = new Date(Date.now() - 2592000_000).toISOString();

let sessions: SessionSummary[] = [
{
  session_id: 'sess-001',
  title: 'Refactor auth middleware to use JWT',
  created_at: HOUR_AGO,
  status: 'running',
  done_reason: null,
  running: true,
  context: {
    repo: 'acme/backend',
    branch: 'feat/auth-refactor',
    mcp_tools: ['tool-fs-read', 'tool-fs-write', 'tool-term-run']
  }
},
{
  session_id: 'sess-002',
  title: 'Fix pagination bug in user list',
  created_at: TWO_HOURS_AGO,
  status: 'completed',
  done_reason: 'completed',
  running: false,
  context: {
    repo: 'acme/web-app',
    branch: 'fix/pagination',
    mcp_tools: ['tool-fs-read', 'tool-fs-write']
  }
},
{
  session_id: 'sess-003',
  title: 'Add dark mode support',
  created_at: YESTERDAY,
  status: 'completed',
  done_reason: 'completed',
  running: false,
  context: { repo: 'acme/web-app', branch: 'feat/dark-mode', mcp_tools: [] }
},
{
  session_id: 'sess-004',
  title: 'Update API rate limiting',
  created_at: THREE_DAYS_AGO,
  status: 'incomplete',
  done_reason: 'canceled',
  running: false,
  context: {
    repo: 'acme/backend',
    branch: 'feat/rate-limit',
    mcp_tools: ['tool-gh-pr']
  }
},
{
  session_id: 'sess-005',
  title: 'Migrate database to PostgreSQL 16',
  created_at: TWO_WEEKS_AGO,
  status: 'completed',
  done_reason: 'completed',
  running: false,
  context: { repo: 'acme/infra', branch: 'chore/pg16', mcp_tools: [] }
}];

let notifications: NotificationItem[] = [
{
  id: 'notif-1',
  title: 'Fix pagination bug in user list',
  message: 'Session completed successfully.',
  level: 'info',
  created_at: new Date(Date.now() - 1800000).toISOString(),
  event_type: 'completed'
},
{
  id: 'notif-2',
  title: 'Update API rate limiting',
  message: 'Session was canceled.',
  level: 'warning',
  created_at: new Date(Date.now() - 7200000).toISOString(),
  event_type: 'canceled'
},
{
  id: 'notif-3',
  title: 'Add dark mode support',
  message: 'Session completed successfully.',
  level: 'info',
  created_at: new Date(Date.now() - 86400000).toISOString(),
  event_type: 'completed'
},
{
  id: 'notif-4',
  title: 'Migrate database to PostgreSQL 16',
  message: 'Session stopped before completion.',
  level: 'warning',
  created_at: new Date(Date.now() - 172800000).toISOString(),
  event_type: 'stopped'
}];


const archivedSessions: SessionSummary[] = [
{
  session_id: 'sess-arc-001',
  title: 'Initial project scaffolding',
  created_at: ONE_MONTH_AGO,
  status: 'completed',
  done_reason: 'completed',
  running: false,
  archived: true,
  context: { repo: 'acme/web-app', branch: 'main', mcp_tools: [] }
},
{
  session_id: 'sess-arc-002',
  title: 'Remove deprecated v1 endpoints',
  created_at: TWO_WEEKS_AGO,
  status: 'completed',
  done_reason: 'completed',
  running: false,
  archived: true,
  context: {
    repo: 'acme/backend',
    branch: 'chore/remove-v1',
    mcp_tools: ['tool-fs-read']
  }
}];


// ---------------------------------------------------------------------------
// Mock events per session (keyed by session_id)
// ---------------------------------------------------------------------------

const DIFF_SAMPLE = `--- a/src/middleware/auth.ts
+++ b/src/middleware/auth.ts
@@ -1,8 +1,12 @@
-import { verifyToken } from '../utils/token';
+import jwt from 'jsonwebtoken';
+import { JWT_SECRET } from '../config';
 
 export function authMiddleware(req, res, next) {
-  const token = req.headers['x-auth-token'];
-  if (!token || !verifyToken(token)) {
-    return res.status(401).json({ error: 'Unauthorized' });
+  const header = req.headers.authorization;
+  if (!header?.startsWith('Bearer ')) {
+    return res.status(401).json({ error: 'Missing token' });
+  }
+  try {
+    req.user = jwt.verify(header.slice(7), JWT_SECRET);
+    next();
+  } catch {
+    return res.status(401).json({ error: 'Invalid token' });
   }
-  next();
 }`;

const DIFF_SAMPLE_2 = `--- a/src/config.ts
+++ b/src/config.ts
@@ -3,3 +3,5 @@
 export const PORT = process.env.PORT || 3000;
 export const DB_URL = process.env.DATABASE_URL || 'postgres://localhost/acme';
+export const JWT_SECRET = process.env.JWT_SECRET || 'dev-secret-change-me';
+export const JWT_EXPIRES_IN = process.env.JWT_EXPIRES_IN || '24h';`;

function makeEvents(sessionId: string): EventRecord[] {
  const base =
  sessions.find((s) => s.session_id === sessionId) ||
  archivedSessions.find((s) => s.session_id === sessionId);
  if (!base) return [];

  const t0 = base.created_at || NOW;
  const t = (offsetMs: number) =>
  new Date(new Date(t0).getTime() + offsetMs).toISOString();

  return [
  // ── Turn 1: User sends an elaborate task ──
  {
    ts: t0,
    type: 'user',
    payload: {
      text:
      'Refactor the auth middleware in `src/middleware/auth.ts` to use JWT tokens instead of the legacy `verifyToken` helper.\n\n' +
      '**Requirements:**\n' +
      '- Replace the custom `x-auth-token` header with the standard `Authorization: Bearer <token>` pattern\n' +
      '- Use the `jsonwebtoken` package for token verification\n' +
      '- Add `JWT_SECRET` and `JWT_EXPIRES_IN` environment variables to `src/config.ts`\n' +
      '- Attach the decoded user payload to `req.user` so downstream handlers can access it\n' +
      '- Make sure all existing tests still pass after the refactor\n' +
      '- Run the linter to confirm no style regressions'
    }
  },

  // ── AI work: planning, searching, editing, testing ──
  {
    ts: t(3000),
    type: 'action_plan',
    payload: {
      steps: [
      {
        title: 'Find existing verifyToken usage',
        description: 'Locate auth middleware references that need JWT updates.'
      },
      {
        title: 'Update auth middleware to JWT',
        description: 'Replace token validation with jsonwebtoken verification.'
      },
      {
        title: 'Add JWT configuration',
        description: 'Introduce JWT_SECRET and JWT_EXPIRES_IN settings.'
      },
      {
        title: 'Run tests and linter',
        description: 'Confirm behavior and style are still correct.'
      }]

    }
  },
  {
    ts: t(8000),
    type: 'tool_result',
    payload: {
      tool_id: 'aider_shell_tool',
      operation: 'run',
      tool_input: { command: "grep -rn 'verifyToken' src/" },
      summary: "Found 2 references to verifyToken in auth middleware.",
      result: JSON.stringify({
        kind: 'shell',
        command: "grep -rn 'verifyToken' src/",
        cwd: '/home/user/workspace/express-api',
        exit_code: 0,
        stdout: "src/middleware/auth.ts:1:import { verifyToken } from '../utils/token';\nsrc/middleware/auth.ts:5:  if (!token || !verifyToken(token)) {",
        stderr: '',
        duration_ms: 45,
      }),
      success: true,
    }
  },
  {
    ts: t(15000),
    type: 'tool_result',
    payload: {
      tool_id: 'write_file',
      operation: 'write',
      tool_input: 'src/middleware/auth.ts',
      summary: 'Rewrote src/middleware/auth.ts with JWT-based authentication',
      result: DIFF_SAMPLE
    }
  },
  {
    ts: t(22000),
    type: 'tool_result',
    payload: {
      tool_id: 'write_file',
      operation: 'write',
      tool_input: 'src/config.ts',
      summary: 'Added JWT_SECRET and JWT_EXPIRES_IN to src/config.ts',
      result: DIFF_SAMPLE_2
    }
  },
  {
    ts: t(28000),
    type: 'step_reflection',
    payload: {
      notes:
      'Auth middleware refactored and config updated. Running test suite and linter to verify nothing is broken.'
    }
  },
  {
    ts: t(35000),
    type: 'tool_result',
    payload: {
      tool_id: 'aider_shell_tool',
      operation: 'run',
      tool_input: { command: 'npm test' },
      summary: 'All 3 tests passed.',
      result: JSON.stringify({
        kind: 'shell',
        command: 'npm test',
        cwd: '/home/user/workspace/express-api',
        exit_code: 0,
        stdout: ' PASS  src/__tests__/auth.test.ts\n  \u2713 rejects requests without token (4ms)\n  \u2713 rejects invalid tokens (2ms)\n  \u2713 passes valid JWT through (3ms)\n\nTest Suites: 1 passed, 1 total\nTests:       3 passed, 3 total',
        stderr: '',
        duration_ms: 3200,
      }),
      success: true,
    }
  },
  {
    ts: t(37000),
    type: 'tool_result',
    payload: {
      tool_id: 'aider_shell_tool',
      operation: 'run',
      tool_input: { command: 'npm run typecheck' },
      summary: 'Type checking failed.',
      result: JSON.stringify({
        kind: 'shell',
        command: 'npm run typecheck',
        cwd: '/home/user/workspace/express-api',
        exit_code: 2,
        stdout: '',
        stderr: "src/middleware/auth.ts(3,7): error TS2322: Type 'string | undefined' is not assignable to type 'string'.\nsrc/config.ts(12,5): error TS2345: Argument of type 'number' is not assignable to parameter of type 'string'.",
        duration_ms: 1200,
      }),
      success: false,
    }
  },
  {
    ts: t(38500),
    type: 'tool_result',
    payload: {
      tool_id: 'aider_shell_tool',
      operation: 'run',
      tool_input: { command: 'docker compose up -d db' },
      summary: 'Command timed out after 120s: docker compose up -d db',
      result: 'Command timed out after 120s: docker compose up -d db',
      success: false,
      error: 'Command timed out after 120s: docker compose up -d db',
    }
  },
  {
    ts: t(40000),
    type: 'tool_result',
    payload: {
      tool_id: 'aider_shell_tool',
      operation: 'run',
      tool_input: { command: 'npm run lint' },
      summary: 'Linter passed with no errors.',
      result: JSON.stringify({
        kind: 'shell',
        command: 'npm run lint',
        cwd: '/home/user/workspace/express-api',
        exit_code: 0,
        stdout: '\u2714 No lint errors found.',
        stderr: '',
        duration_ms: 890,
      }),
      success: true,
    }
  },

  // ── Permission + Sub-agent lifecycle ──
  {
    ts: t(41000),
    type: 'permission',
    payload: {
      tool_id: 'write_file',
      operation: 'set',
      tool_input: { path: 'src/middleware/auth.ts', content: '/* JWT auth middleware */' },
      decision: 'allow',
    }
  },
  {
    ts: t(41500),
    type: 'permission',
    payload: {
      tool_id: 'shell',
      operation: 'set',
      tool_input: { command: 'rm -rf /tmp/cache' },
      decision: 'deny',
    }
  },
  {
    ts: t(42000),
    type: 'sub_agent',
    payload: {
      action: 'start',
      agent_id: 'a3f2bc91e17f',
      parent_id: null,
      depth: 1,
      model: 'openai/claude-sonnet-4-6',
      detail: 'Run integration tests and verify JWT authentication flow works end-to-end',
      status: 'running',
      steps_completed: 0,
    }
  },
  {
    ts: t(43500),
    type: 'sub_agent',
    payload: {
      action: 'stop',
      agent_id: 'a3f2bc91e17f',
      parent_id: null,
      depth: 1,
      model: 'openai/claude-sonnet-4-6',
      detail: 'completed',
      status: 'completed',
      steps_completed: 5,
    }
  },
  {
    ts: t(44000),
    type: 'tool_result',
    payload: {
      tool_id: 'spawn_agent',
      operation: 'set',
      tool_input: { task: 'Run integration tests for JWT auth' },
      result: JSON.stringify({
        content: 'All tests passed. JWT auth middleware is working correctly.',
        status: 'completed',
        steps_used: 5,
        summary: 'Ran 3 integration tests for JWT auth. All passed.',
        artifacts: ['src/__tests__/auth.test.ts'],
        warnings: [],
      }),
      success: true,
    }
  },

  // ── AI responds with a complete summary ──
  {
    ts: t(45000),
    type: 'assistant',
    payload: {
      text:
      'Done — the auth middleware now uses **JWT tokens**.\n\n' +
      '### What I changed\n' +
      '- **`src/middleware/auth.ts`** — Replaced the legacy `verifyToken` helper with `jsonwebtoken`. The middleware now reads the `Authorization: Bearer <token>` header, verifies it with `jwt.verify()`, and attaches the decoded payload to `req.user`.\n' +
      '- **`src/config.ts`** — Added `JWT_SECRET` and `JWT_EXPIRES_IN` environment variables with sensible defaults for local development.\n\n' +
      '### Verification\n' +
      '- **Tests:** All 3 existing tests pass (`npm test`)\n' +
      '- **Lint:** No style regressions (`npm run lint`)\n\n' +
      "You can merge this as-is or let me know if you'd like any adjustments."
    }
  },

  // ── Completion ──
  {
    ts: t(46000),
    type: 'completion',
    payload: {
      done: true,
      done_reason: 'completed',
    }
  }];

}

// ---------------------------------------------------------------------------
// Mock MCP tools
// ---------------------------------------------------------------------------

const mcpTools: ToolSummary[] = [
{
  tool_id: 'tool-fs-read',
  name: 'read_file',
  kind: 'mcp',
  enabled: true,
  description: 'Read file contents',
  server: 'filesystem'
},
{
  tool_id: 'tool-fs-write',
  name: 'write_file',
  kind: 'mcp',
  enabled: true,
  description: 'Write to a file',
  server: 'filesystem'
},
{
  tool_id: 'tool-fs-list',
  name: 'list_directory',
  kind: 'mcp',
  enabled: true,
  description: 'List directory contents',
  server: 'filesystem'
},
{
  tool_id: 'tool-gh-pr',
  name: 'create_pr',
  kind: 'mcp',
  enabled: true,
  description: 'Create a pull request',
  server: 'github'
},
{
  tool_id: 'tool-gh-issues',
  name: 'list_issues',
  kind: 'mcp',
  enabled: true,
  description: 'List repository issues',
  server: 'github'
},
{
  tool_id: 'tool-term-run',
  name: 'run_command',
  kind: 'mcp',
  enabled: true,
  description: 'Execute a shell command',
  server: 'terminal'
},
{
  tool_id: 'tool-db-query',
  name: 'query',
  kind: 'mcp',
  enabled: false,
  disabled_reason: 'No database configured',
  description: 'Run SQL queries',
  server: 'database'
}];


// ---------------------------------------------------------------------------
// Mock API implementations (same signatures as client.ts exports)
// ---------------------------------------------------------------------------

let sessionCounter = 100;

export async function mockListSessions(
includeArchived = false)
: Promise<SessionSummary[]> {
  await delay(150);
  if (includeArchived) {
    return [...sessions, ...archivedSessions];
  }
  return sessions.filter((s) => !s.archived);
}

export async function mockCreateSession(
context?: SessionContext)
: Promise<string> {
  await delay(200);
  sessionCounter += 1;
  const id = `sess-new-${sessionCounter}`;
  sessions = [
  {
    session_id: id,
    title: 'New task',
    created_at: new Date().toISOString(),
    status: 'running',
    done_reason: null,
    running: true,
    context: context || {}
  },
  ...sessions];

  return id;
}

export async function mockPostQuery(
sessionId: string,
query: string,
_context?: SessionContext,
_mode?: QueryMode,
_attachments?: AttachmentPayload[])
: Promise<void> {
  void _context;
  void _mode;
  void _attachments;
  await delay(100);
  // Update the session title to the query text (simulates backend behavior)
  sessions = sessions.map((s) =>
  s.session_id === sessionId ?
  { ...s, title: query === '/terminate' ? s.title : query } :
  s
  );
}

const eventCache = new Map<string, EventRecord[]>();

export async function mockFetchEvents(
sessionId: string,
after?: string)
: Promise<{events: EventRecord[];running: boolean;}> {
  await delay(100);
  if (!eventCache.has(sessionId)) {
    eventCache.set(sessionId, makeEvents(sessionId));
  }
  const allEvents = eventCache.get(sessionId) || [];

  // If polling with `after`, return empty (all events already delivered)
  if (after) {
    return { events: [], running: false };
  }
  return { events: allEvents, running: false };
}

export async function mockArchiveSession(sessionId: string): Promise<void> {
  await delay(100);
  const idx = sessions.findIndex((s) => s.session_id === sessionId);
  if (idx !== -1) {
    const [removed] = sessions.splice(idx, 1);
    archivedSessions.push({ ...removed, archived: true });
  }
}

export async function mockUnarchiveSession(sessionId: string): Promise<void> {
  await delay(100);
  const idx = archivedSessions.findIndex((s) => s.session_id === sessionId);
  if (idx !== -1) {
    const [removed] = archivedSessions.splice(idx, 1);
    sessions.unshift({ ...removed, archived: false });
  }
}

export async function mockUpdateSessionTitle(
  sessionId: string,
  title: string
): Promise<{ session_id: string; title: string }> {
  await delay(50);
  const active = sessions.find((s) => s.session_id === sessionId);
  if (active) active.title = title;
  const archived = archivedSessions.find((s) => s.session_id === sessionId);
  if (archived) archived.title = title;
  return { session_id: sessionId, title };
}

export async function mockListTools(): Promise<ToolSummary[]> {
  await delay(100);
  return mcpTools;
}

const mockSkills: SkillSummary[] = [
  {
    name: "review-pr",
    description: "Review a GitHub pull request for code quality and correctness",
    allowed_tools: null,
    user_invocable: true,
    disable_model_invocation: false,
    context: null,
    source: "project",
  },
  {
    name: "commit",
    description: "Create a conventional commit with Gitmoji prefix",
    allowed_tools: null,
    user_invocable: true,
    disable_model_invocation: true,
    context: null,
    source: "personal",
  },
];

export async function mockListSkills(): Promise<SkillSummary[]> {
  await delay(100);
  return mockSkills;
}

export async function mockListNotifications(): Promise<NotificationItem[]> {
  await delay(100);
  return notifications;
}

export async function mockDismissNotification(
ids: string[])
: Promise<void> {
  await delay(100);
  notifications = notifications.filter((n) => !ids.includes(n.id));
}

export async function mockClearNotifications(
_clearAll?: boolean)
: Promise<void> {
  await delay(100);
  notifications = [];
}

export async function mockUploadAttachments(
_sessionId: string,
files: File[])
: Promise<AttachmentRecord[]> {
  await delay(100);
  return files.map((file, idx) => ({
    id: `att-${idx + 1}`,
    filename: file.name,
    stored_name: file.name,
    content_type: file.type,
    size_bytes: file.size,
    uploaded_at: new Date().toISOString()
  }));
}

export async function mockCreateShare(sessionId: string): Promise<ShareRecord> {
  await delay(50);
  return {
    token: `share-${sessionId.slice(0, 6)}`,
    session_id: sessionId,
    created_at: new Date().toISOString()
  };
}

export async function mockExportSession(sessionId: string): Promise<SessionExport> {
  await delay(100);
  return {
    session_id: sessionId,
    events: eventCache.get(sessionId) || [],
    summary: null
  };
}

export async function mockResolveShare(token: string): Promise<SessionExport> {
  await delay(100);
  return {
    token,
    session_id: token.replace("share-", "sess-"),
    events: [],
    summary: null
  };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
