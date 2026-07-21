/**
 * The identity-administration wire surface — `/api/iam/*`.
 *
 * Mirrors the Pydantic view models in `mewbo_api/iam/routes.py`; when a shape
 * here and a `*View` there disagree, that file wins.
 *
 * Two behaviours of that surface shape this whole module:
 *
 * - **The blueprint 404s wholesale while auth is off.** A `before_request` gate
 *   answers "identity and access management is not enabled" without touching a
 *   store. So a 404 is a deployment fact ("this Mewbo has no identity to
 *   administer"), not a failure, and it is classified into
 *   `EndpointUnavailableError` for the panes to state plainly.
 * - **Lists are paginated** as `{items, total, limit, offset}`. Callers here
 *   take `items` and surface `total`, rather than inventing a rows array.
 *
 * SCIM (`/scim/v2/*`) is deliberately NOT consumed: it is a machine-to-machine
 * provisioning surface with its own bearer token, so the console cannot call it
 * as the signed-in user.
 */
import { API_BASE } from "./client";
import { apiFetch, authHeaders, jsonHeaders, readError, withBase as sharedWithBase } from "./httpBase";

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/** Identity management is not enabled (or not served) on this deployment. */
export class EndpointUnavailableError extends Error {
  constructor(path: string) {
    super(`This deployment does not serve ${path}.`);
    this.name = "EndpointUnavailableError";
  }
}

/** The caller's role does not carry the permission this route requires. */
export class ForbiddenError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ForbiddenError";
  }
}

async function classify(response: Response, path: string): Promise<never> {
  if (response.status === 404 || response.status === 501) {
    throw new EndpointUnavailableError(path);
  }
  if (response.status === 403) {
    throw new ForbiddenError((await readError(response)).message);
  }
  throw await readError(response);
}

async function getJson<T>(path: string): Promise<T> {
  const response = await apiFetch(withBase(path), { headers: authHeaders() });
  if (!response.ok) await classify(response, path);
  return (await response.json()) as T;
}

async function sendJson<T>(method: string, path: string, body?: unknown): Promise<T> {
  const response = await apiFetch(withBase(path), {
    method,
    headers: jsonHeaders(),
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) await classify(response, path);
  return (response.status === 204 ? ({} as T) : ((await response.json()) as T));
}

/** The paginated envelope every IAM list route answers with. */
export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

// ---------------------------------------------------------------------------
// Users — permission: users.admin
// ---------------------------------------------------------------------------

export interface ExternalIdentity {
  issuer: string;
  subject: string;
}

export interface IamUser {
  id: string;
  email: string | null;
  email_verified: boolean | null;
  display_name: string | null;
  status: "active" | "disabled";
  roles: string[];
  external_identities: ExternalIdentity[];
  avatar: { picture_url: string | null; gravatar_url: string | null };
  created_at: string;
  updated_at: string;
}

export function listUsers(q?: string): Promise<Page<IamUser>> {
  const query = q?.trim() ? `?q=${encodeURIComponent(q.trim())}` : "";
  return getJson<Page<IamUser>>(`/api/iam/users${query}`);
}

/**
 * Reassign roles and/or set account status.
 *
 * Every other user field is provider-owned and is overwritten on the next
 * federated login or SCIM push, which is why the route accepts only these two.
 */
export function patchUser(
  userId: string,
  patch: { roles?: string[]; status?: "active" | "disabled" },
): Promise<IamUser> {
  return sendJson<IamUser>("PATCH", `/api/iam/users/${encodeURIComponent(userId)}`, patch);
}

// ---------------------------------------------------------------------------
// Teams — permission: teams.admin
// ---------------------------------------------------------------------------

export interface IamTeam {
  id: string;
  /** The mapping target; immutable once created. */
  slug: string;
  name: string;
  description: string;
}

export function listTeams(): Promise<Page<IamTeam>> {
  return getJson<Page<IamTeam>>("/api/iam/teams");
}

export function createTeam(input: {
  slug: string;
  name: string;
  description?: string;
}): Promise<IamTeam> {
  return sendJson<IamTeam>("POST", "/api/iam/teams", input);
}

/**
 * Rename or re-describe a team. `slug` is deliberately absent: group→team
 * mapping rules target a team BY SLUG, so renaming one would silently detach
 * every rule pointing at it.
 */
export function patchTeam(
  teamId: string,
  patch: { name?: string; description?: string },
): Promise<IamTeam> {
  return sendJson<IamTeam>("PATCH", `/api/iam/teams/${encodeURIComponent(teamId)}`, patch);
}

export function deleteTeam(teamId: string): Promise<unknown> {
  return sendJson("DELETE", `/api/iam/teams/${encodeURIComponent(teamId)}`);
}

// ---------------------------------------------------------------------------
// Roles + the permission catalog — permission: roles.admin
// ---------------------------------------------------------------------------

export interface IamRole {
  name: string;
  description: string;
  permissions: string[];
  /** Built-in roles are read-only. */
  builtin: boolean;
}

export function listRoles(): Promise<Page<IamRole>> {
  return getJson<Page<IamRole>>("/api/iam/roles");
}

export function createRole(input: {
  name: string;
  description?: string;
  permissions: string[];
}): Promise<IamRole> {
  return sendJson<IamRole>("POST", "/api/iam/roles", input);
}

export function patchRole(
  name: string,
  patch: { description?: string; permissions?: string[] },
): Promise<IamRole> {
  return sendJson<IamRole>("PATCH", `/api/iam/roles/${encodeURIComponent(name)}`, patch);
}

export function deleteRole(name: string): Promise<unknown> {
  return sendJson("DELETE", `/api/iam/roles/${encodeURIComponent(name)}`);
}

/** One product domain and every permission id inside it. */
export interface PermissionDomain {
  domain: string;
  permissions: string[];
}

export interface PermissionCatalog {
  domains: PermissionDomain[];
  total: number;
}

/**
 * The closed permission catalog, grouped for a role editor.
 *
 * The console must never keep its own copy: `mewbo_iam`'s `PermissionCatalog`
 * is the source of truth, and a hand-maintained duplicate goes stale the first
 * time a permission is added — the trap the settings surface already hit when
 * it hardcoded a copy of one of these lists.
 */
export function fetchPermissionCatalog(): Promise<PermissionCatalog> {
  return getJson<PermissionCatalog>("/api/iam/permissions");
}

// ---------------------------------------------------------------------------
// Provider group mappings — permission: roles.admin
// ---------------------------------------------------------------------------

/** One configured group→target rule, in evaluation order. */
export interface MappingRule {
  match: string;
  /** How `match` is compared. */
  match_kind: "exact" | "regex";
  /** Role name or team slug conferred on a match. */
  target: string;
}

export interface BootstrapRule {
  admin_group: string | null;
  admin_subjects: string[];
}

/** Why a federated user got the roles they got. Config-derived, read-only. */
export interface MappingsResponse {
  default_role: string;
  role_rules: MappingRule[];
  team_rules: MappingRule[];
  bootstrap: BootstrapRule | null;
}

export function fetchMappings(): Promise<MappingsResponse> {
  return getJson<MappingsResponse>("/api/iam/mappings");
}

// ---------------------------------------------------------------------------
// Audit trail — permission: audit.read
// ---------------------------------------------------------------------------

/**
 * One audit event.
 *
 * The server types this as a discriminated union on `type`, each variant
 * carrying only its own fields. Rather than mirror all nine variants for a
 * table that renders a summary line, this models the shared envelope and keeps
 * the variant-specific fields as an index signature — `AuditCard` reads `type`
 * to build the summary and never assumes a field exists. Mirroring the full
 * union would be worth it the day a per-kind detail view exists.
 */
export interface IamAuditEvent {
  type: string;
  ts: string;
  actor_subject: string | null;
  source: "api" | "console" | "mcp" | "scim";
  [field: string]: unknown;
}

export function listAudit(limit = 100): Promise<Page<IamAuditEvent>> {
  return getJson<Page<IamAuditEvent>>(`/api/iam/audit?limit=${limit}`);
}
