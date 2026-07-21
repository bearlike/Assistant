/**
 * TanStack hooks over the identity-administration surface (`api/iam.ts`).
 *
 * Every read shares one shape via `useIamResource`, because every IAM card asks
 * the same three questions before it can render: may I see this, does this
 * deployment serve it, and did it work. Collapsing those into one `IamQuery`
 * result is what keeps each card's render a straight-line read instead of five
 * copies of the same branch ladder.
 *
 * `retry: false` throughout: an absent surface and a forbidden one are both
 * settled answers, and retrying either only delays the honest state.
 */
import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  createRole,
  createTeam,
  deleteRole,
  deleteTeam,
  EndpointUnavailableError,
  fetchMappings,
  fetchPermissionCatalog,
  ForbiddenError,
  listAudit,
  listRoles,
  listTeams,
  listUsers,
  patchRole,
  patchTeam,
  patchUser,
  type IamAuditEvent,
  type IamRole,
  type IamTeam,
  type IamUser,
  type MappingsResponse,
  type PermissionCatalog,
} from "../api/iam";

export const IAM_KEY = ["iam"] as const;

export interface IamQuery<T> {
  data: T | null;
  loading: boolean;
  /** Identity management is off, or this build does not serve the route. */
  unavailable: boolean;
  /** The caller's role lacks the required permission. */
  forbidden: boolean;
  /** A real failure — neither of the two settled states above. */
  error: Error | null;
  /**
   * Whether a write affordance should render at all.
   *
   * Neither settled refusal is recoverable by trying, so a "New role" button
   * above an unavailable or forbidden list would only offer a guaranteed
   * failure. Derived here rather than at each card, because both cards that ask
   * had spelled out the same two-clause rule.
   */
  writable: boolean;
}

function useIamResource<T>(
  key: readonly unknown[],
  queryFn: () => Promise<T>,
  enabled = true,
): IamQuery<T> {
  const query = useQuery<T>({ queryKey: key, queryFn, enabled, retry: false, staleTime: 60_000 });
  const err = query.error;
  const settled = err instanceof EndpointUnavailableError || err instanceof ForbiddenError;
  return {
    data: query.data ?? null,
    // `isLoading` (not `isPending`): a DISABLED query stays pending forever,
    // which would pin a permission-gated card in a permanent spinner.
    loading: query.isLoading,
    unavailable: err instanceof EndpointUnavailableError,
    forbidden: err instanceof ForbiddenError,
    error: settled ? null : err instanceof Error ? err : null,
    writable: !settled,
  };
}

export function useIamUsers(q?: string) {
  return useIamResource([...IAM_KEY, "users", q ?? ""], () => listUsers(q));
}

export function useIamTeams() {
  return useIamResource([...IAM_KEY, "teams"], listTeams);
}

export function useIamRoles() {
  return useIamResource([...IAM_KEY, "roles"], listRoles);
}

/**
 * The catalog is the one IAM read that is genuinely conditional: it backs the
 * role editor's checkbox list and is fetched only while that dialog is open.
 */
export function useIamPermissionCatalog(enabled: boolean): IamQuery<PermissionCatalog> {
  return useIamResource([...IAM_KEY, "permissions"], fetchPermissionCatalog, enabled);
}

export function useIamMappings(): IamQuery<MappingsResponse> {
  return useIamResource([...IAM_KEY, "mappings"], fetchMappings);
}

export function useIamAudit() {
  return useIamResource([...IAM_KEY, "audit"], () => listAudit());
}

// ---------------------------------------------------------------------------
// Writes
// ---------------------------------------------------------------------------

/**
 * Every IAM write invalidates the whole `["iam"]` subtree rather than one list.
 *
 * These records reference each other — disabling a user shows up in the audit
 * trail, deleting a role changes what the user rows display — so a
 * write-then-invalidate-one-key would leave a sibling card contradicting the
 * one that just changed. The lists are small and admin-frequency; a broad
 * invalidation is the cheap correct answer.
 */
function useIamInvalidate() {
  const qc = useQueryClient();
  return useCallback(() => {
    void qc.invalidateQueries({ queryKey: IAM_KEY });
  }, [qc]);
}

export function useUpdateUser() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (input: {
      userId: string;
      patch: { roles?: string[]; status?: "active" | "disabled" };
    }) => patchUser(input.userId, input.patch),
    onSuccess: invalidate,
  });
}

export function useCreateTeam() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (input: { slug: string; name: string; description?: string }) => createTeam(input),
    onSuccess: invalidate,
  });
}

export function useUpdateTeam() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (input: { teamId: string; patch: { name?: string; description?: string } }) =>
      patchTeam(input.teamId, input.patch),
    onSuccess: invalidate,
  });
}

export function useDeleteTeam() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (teamId: string) => deleteTeam(teamId),
    onSuccess: invalidate,
  });
}

export function useCreateRole() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (input: { name: string; description?: string; permissions: string[] }) =>
      createRole(input),
    onSuccess: invalidate,
  });
}

export function useUpdateRole() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (input: {
      name: string;
      patch: { description?: string; permissions?: string[] };
    }) => patchRole(input.name, input.patch),
    onSuccess: invalidate,
  });
}

export function useDeleteRole() {
  const invalidate = useIamInvalidate();
  return useMutation({
    mutationFn: (name: string) => deleteRole(name),
    onSuccess: invalidate,
  });
}

export type { IamAuditEvent, IamRole, IamTeam, IamUser };
