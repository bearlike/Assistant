import { API_BASE, API_KEY } from "./client";

export type IdeStatus = "pending" | "starting" | "ready";

export interface IdeInstance {
  session_id: string;
  status: IdeStatus;
  url: string;
  project_name: string;
  project_path: string;
  /** Only present on POST responses; GET/extend omit it to avoid re-sending the secret. */
  password?: string;
  created_at: string;
  expires_at: string;
  max_deadline: string;
  remaining_seconds: number;
  extensions: number;
  cpus: number;
  memory: string;
}

export interface IdeErrorBody {
  error?: string;
  message?: string;
  max_deadline?: string;
}

export class IdeApiError extends Error {
  status: number;
  body: IdeErrorBody;
  constructor(status: number, message: string, body: IdeErrorBody) {
    super(message);
    this.status = status;
    this.body = body;
  }
}

function withBase(path: string): string {
  if (!API_BASE) return path;
  return `${API_BASE.replace(/\/$/, "")}${path}`;
}

function jsonHeaders(): HeadersInit {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (API_KEY) h["X-API-Key"] = API_KEY;
  return h;
}

function authHeaders(): HeadersInit {
  return API_KEY ? { "X-API-Key": API_KEY } : {};
}

async function parseError(response: Response): Promise<IdeApiError> {
  const text = await response.text();
  let body: IdeErrorBody = {};
  if (text.trim()) {
    try {
      body = JSON.parse(text) as IdeErrorBody;
    } catch {
      body = { message: text };
    }
  }
  const message =
    body.error || body.message || text || `Request failed: ${response.status}`;
  return new IdeApiError(response.status, message, body);
}

async function readJson<T>(response: Response): Promise<T> {
  if (!response.ok) throw await parseError(response);
  const text = await response.text();
  return text ? (JSON.parse(text) as T) : ({} as T);
}

/** POST /api/sessions/<sid>/ide — create or reconnect. */
export async function createIde(
  sessionId: string,
  signal?: AbortSignal
): Promise<IdeInstance> {
  const response = await fetch(withBase(`/api/sessions/${sessionId}/ide`), {
    method: "POST",
    headers: jsonHeaders(),
    signal,
  });
  return readJson<IdeInstance>(response);
}

/** GET /api/sessions/<sid>/ide — current state, or null if none. */
export async function getIde(
  sessionId: string,
  signal?: AbortSignal
): Promise<IdeInstance | null> {
  const response = await fetch(withBase(`/api/sessions/${sessionId}/ide`), {
    headers: authHeaders(),
    signal,
  });
  if (response.status === 404) return null;
  return readJson<IdeInstance>(response);
}

/** POST /api/sessions/<sid>/ide/extend. Body is one of {hours} or {expires_at}. */
export async function extendIde(
  sessionId: string,
  body: { hours: number } | { expires_at: string },
  signal?: AbortSignal
): Promise<IdeInstance> {
  const response = await fetch(
    withBase(`/api/sessions/${sessionId}/ide/extend`),
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(body),
      signal,
    }
  );
  return readJson<IdeInstance>(response);
}

/** DELETE /api/sessions/<sid>/ide — 204 on success, 404 if nothing to delete. */
export async function stopIde(
  sessionId: string,
  signal?: AbortSignal
): Promise<boolean> {
  const response = await fetch(withBase(`/api/sessions/${sessionId}/ide`), {
    method: "DELETE",
    headers: authHeaders(),
    signal,
  });
  if (response.status === 204) return true;
  if (response.status === 404) return false;
  throw await parseError(response);
}
