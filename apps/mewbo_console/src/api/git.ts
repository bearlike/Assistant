/**
 * Product-wide git credential management — `/v1/git/credentials`.
 *
 * The wiki indexer is the first consumer, but the credential chain (stored
 * credential → ambient git credential) is shared by every git-touching flow
 * (wiki, tasks, vcs pickup). A credential is keyed by a plain `scope` string:
 *
 *   - `host` scope  = a bare host (`git.example.com`) — shared by every repo on
 *     that host.
 *   - `repo` scope  = a full slug (`host/owner/repo`) — pinned to one repo.
 *
 * The raw secret is write-only: the API never returns it (only a `valueHint`),
 * and the client must clear it from state right after a submit.
 */
import { API_BASE } from "./client";
import { apiFetch, withBase as sharedWithBase, authHeaders, jsonHeaders, readJson, readError } from "./httpBase";

export type GitCredentialScopeType = "host" | "repo";
export type GitCredentialKind = "token" | "ssh_key";

/** One row of `GET /v1/git/credentials` — never carries the secret value. */
export interface GitCredentialSummary {
  scope: string;
  scopeType: GitCredentialScopeType;
  kind: GitCredentialKind;
  username: string | null;
  /** `"…"+value[-4:]` for tokens, `"ssh key"` for ssh_key. Never the value. */
  valueHint: string;
  updatedAt: string | null;
}

/** Body for `PUT /v1/git/credentials/<scope>`. `value` is the raw secret. */
export interface GitCredentialInput {
  kind: GitCredentialKind;
  value: string;
  username?: string;
}

/** Result of `POST /v1/git/credentials/<scope>/validate`. */
export interface GitCredentialValidateResult {
  ok: boolean;
  detail: string;
}

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/**
 * Encode a scope for use in the URL. Mirrors the wiki client's proven
 * `encodeURIComponent(slug)` convention for the sibling `<path:slug>` routes
 * (e.g. `GET /v1/wiki/projects/<slug>/source`): the whole scope is encoded —
 * the `%2F`-encoded slashes are decoded back by the Flask `<path:scope>`
 * converter.
 */
function encodeScope(scope: string): string {
  return encodeURIComponent(scope);
}

/** GET /v1/git/credentials — the credential list (no secret values). */
export async function listGitCredentials(
  signal?: AbortSignal
): Promise<GitCredentialSummary[]> {
  const response = await apiFetch(withBase("/v1/git/credentials"), {
    headers: authHeaders(),
    signal,
  });
  const data = await readJson<{ credentials?: GitCredentialSummary[] }>(response);
  return data.credentials ?? [];
}

/** PUT /v1/git/credentials/<scope> — create or replace a credential. */
export async function putGitCredential(
  scope: string,
  input: GitCredentialInput,
  signal?: AbortSignal
): Promise<void> {
  const response = await apiFetch(withBase(`/v1/git/credentials/${encodeScope(scope)}`), {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
    signal,
  });
  if (!response.ok) throw await readError(response);
}

/** DELETE /v1/git/credentials/<scope>. A 404 means it's already gone. */
export async function deleteGitCredential(
  scope: string,
  signal?: AbortSignal
): Promise<void> {
  const response = await apiFetch(withBase(`/v1/git/credentials/${encodeScope(scope)}`), {
    method: "DELETE",
    headers: authHeaders(),
    signal,
  });
  if (response.ok || response.status === 404) return;
  throw await readError(response);
}

/**
 * POST /v1/git/credentials/<scope>/validate — a real `git ls-remote` with the
 * stored credential injected. Host scopes require a `repoUrl` (there's no repo
 * to probe otherwise); repo scopes default it to `https://<scope>`.
 */
export async function validateGitCredential(
  scope: string,
  repoUrl?: string,
  signal?: AbortSignal
): Promise<GitCredentialValidateResult> {
  const response = await apiFetch(
    withBase(`/v1/git/credentials/${encodeScope(scope)}/validate`),
    {
      method: "POST",
      headers: jsonHeaders(),
      body: JSON.stringify(repoUrl ? { repoUrl } : {}),
      signal,
    }
  );
  return readJson<GitCredentialValidateResult>(response);
}

// ---------------------------------------------------------------------------
// URL → host / slug derivation (mirrors the backend `host_of`).
// ---------------------------------------------------------------------------

/**
 * Extract the host from a git URL or slug. Mirrors the backend `host_of`:
 * `https://git.h.home/o/r(.git)` → `git.h.home`; a bare slug `git.h.home/o/r`
 * → `git.h.home`. Returns `null` when nothing host-like can be parsed.
 */
export function hostOf(urlOrSlug: string): string | null {
  const raw = urlOrSlug.trim();
  if (!raw) return null;
  // scp-like syntax: git@host:owner/repo
  const scp = raw.match(/^[^@\s]+@([^:/\s]+):/);
  if (scp) return scp[1].toLowerCase();
  try {
    const u = new URL(raw);
    return u.host.toLowerCase() || null;
  } catch {
    // Bare slug `host/owner/repo` — the first path segment is the host.
    const first = raw.replace(/^\/+/, "").split("/")[0];
    return first ? first.toLowerCase() : null;
  }
}

/**
 * Classify a scope string as a bare host (`git.example.com`) or a full
 * `host/owner/repo` slug — the one place this rule is spelled out so the
 * add/edit form's live preview (`CredentialDialog`) doesn't re-derive it
 * inline from an ad hoc `/` check.
 */
export function scopeTypeOf(scope: string): GitCredentialScopeType {
  return scope.trim().includes("/") ? "repo" : "host";
}

/**
 * Which stored credential covers a repo — repo scope wins over host scope,
 * mirroring the backend chain's precedence. The ONE client-side matcher: the
 * onboarding wizard uses it to hint "a saved credential already covers this
 * repo" (no server answer exists yet — the project isn't created), and the
 * project-settings dialog uses it only as a fallback for a settings payload
 * that omits the server-resolved `credential` field.
 *
 * Where the server DOES answer (the settings DTO), prefer that: only the
 * backend walks the real chain, and a client-side guess can disagree with it.
 */
export function matchCredential(
  credentials: GitCredentialSummary[],
  identity: { slug?: string | null; host?: string | null },
): GitCredentialSummary | null {
  const { slug, host } = identity;
  return (
    (slug ? credentials.find((c) => c.scope === slug) : undefined) ??
    (host ? credentials.find((c) => c.scope === host) : undefined) ??
    null
  );
}
