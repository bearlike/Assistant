/**
 * The console's one auth-session seam.
 *
 * `useAuthSession()` is the ONLY way a component learns who is signed in, what
 * they may do, and whether this deployment has auth turned on at all. Nothing
 * else may call `/api/auth/me` — the TanStack cache under `AUTH_SESSION_KEY` is
 * the single source of truth, so the rail, the login gate and the admin panes
 * all read one fetch.
 *
 * ## The auth-disabled contract
 *
 * With auth off, `/me` returns the legacy full-power principal, so `session`
 * is non-null, `authEnabled` is false, and `can()` answers true for everything.
 * Consumers therefore render exactly as they did before this layer existed —
 * no feature flag, no branch, no second code path to keep alive. A consumer
 * that gates a surface on `authEnabled` is almost always wrong; gate on
 * `can()`, which already collapses to "yes" in that mode.
 */
import { useCallback, useMemo } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";

import { fetchMe, type MeResponse, type MeResult } from "../api/auth";

export const AUTH_SESSION_KEY = ["auth", "me"] as const;

// ---------------------------------------------------------------------------
// Identity derivations — pure, and shared with every other principal shape
// ---------------------------------------------------------------------------

/**
 * `AuthSession` is not the only place a principal is rendered: the People table
 * shows `IamUser` records carrying the same name/email/avatar triple under
 * different field names. Both surfaces have to agree, so the rules live here
 * once and the getters below delegate. A second copy is how the rail and the
 * People table came to show different initials for the same person.
 */

/** Best available human name, never empty. */
export function displayNameFrom(profile: {
  display?: string | null;
  email?: string | null;
  subject: string;
}): string {
  const display = profile.display?.trim();
  if (display) return display;
  const email = profile.email?.trim();
  if (email) return email;
  return profile.subject;
}

/**
 * One or two letters for the fallback avatar. Prefers the initials of a
 * multi-word name, then the first characters of whatever identifier we do have
 * — a principal always renders as something.
 */
export function initialsFrom(name: string): string {
  const words = name.split(/[\s._-]+/).filter(Boolean);
  if (words.length >= 2) {
    return (words[0][0] + words[1][0]).toUpperCase();
  }
  const bare = name.replace(/^[^a-z0-9]+/i, "");
  return (bare.slice(0, 2) || name.slice(0, 2) || "?").toUpperCase();
}

/** The avatar image candidates, highest precedence first. */
export function avatarSourcesFrom(avatar: {
  picture_url: string | null;
  gravatar_url: string | null;
}): string[] {
  return [avatar.picture_url, avatar.gravatar_url].filter((url): url is string => Boolean(url));
}

/**
 * The permission ids the console itself gates a surface on.
 *
 * Deliberately NOT a mirror of the server's catalog: the role editor sources
 * that from `GET /api/iam/permissions` precisely so it can never go stale.
 * This union is only the handful of ids written into console gates, narrowed
 * so that a typo is a compile error instead of a silent `false`.
 */
export type ConsolePermission =
  | "users.admin"
  | "teams.admin"
  | "roles.admin"
  | "audit.read"
  | "keys.admin";

/**
 * The signed-in caller, plus the permission question every gated surface asks.
 *
 * Behaviour that is intrinsic to the profile lives HERE rather than being
 * re-derived at each call site — the avatar chain, the initials, the display
 * fallbacks and `can()` are all facts about a principal, not about a component.
 */
export class AuthSession {
  constructor(private readonly profile: MeResponse) {}

  get subject(): string {
    return this.profile.subject;
  }

  get email(): string | null {
    return this.profile.email;
  }

  get roles(): string[] {
    return this.profile.roles;
  }

  get teams() {
    return this.profile.teams;
  }

  get authMethod() {
    return this.profile.auth_method;
  }

  get authEnabled(): boolean {
    return this.profile.auth_enabled;
  }

  /**
   * Whether this is the placeholder principal a no-auth deployment runs as,
   * rather than a real signed-in person. The user menu says so out loud instead
   * of inventing an identity for it.
   */
  get isLegacyPrincipal(): boolean {
    return !this.profile.auth_enabled;
  }

  /** Best available human name, never empty. */
  get displayName(): string {
    return displayNameFrom(this.profile);
  }

  /** The avatar image candidates, highest precedence first. */
  get avatarSources(): string[] {
    return avatarSourcesFrom(this.profile.avatar);
  }

  /** One or two letters for the fallback avatar. */
  get initials(): string {
    return initialsFrom(this.displayName);
  }

  /**
   * Whether the caller holds a permission id (`users.admin`, `keys.admin`, …).
   *
   * **This is UX, never security.** The API enforces every one of these;
   * hiding a control the caller cannot use keeps the surface honest, but a
   * hidden control is not a protected one.
   *
   * `permissions` is the RESOLVED set from `/me` — the server unions built-in
   * and custom role grants, which is the only correct answer. The console
   * deliberately does not derive it from `roles`: a custom role's grants live
   * in the role store, so resolving names here would both duplicate the
   * permission catalog and miss every role an operator defines.
   *
   * The field is absent only against an API older than the change that added
   * it. That is unknowable rather than false, so it resolves to "offer it" and
   * lets the server adjudicate — every gated surface already renders a 403 as
   * a clean "your role does not include permission to view this". Guessing
   * "no" would hide a working panel and state a false reason for hiding it,
   * which is the failure worth avoiding.
   */
  can(permission: ConsolePermission): boolean {
    if (!this.profile.auth_enabled) return true;
    const granted = this.profile.permissions;
    if (!granted) return true;
    return granted.includes(permission);
  }
}

export interface AuthSessionState {
  /** The signed-in caller, or null while loading / when anonymous. */
  session: AuthSession | null;
  /** Whether this deployment requires sign-in at all. */
  authEnabled: boolean;
  /** Auth is on and nobody is signed in — the login screen's cue. */
  isAnonymous: boolean;
  loading: boolean;
  /** A transport failure. An anonymous 401 is NOT an error. */
  error: Error | null;
  /** Permission check, false while the profile is still loading. */
  can: (permission: ConsolePermission) => boolean;
  refresh: () => Promise<void>;
}

export function useAuthSession(): AuthSessionState {
  const qc = useQueryClient();
  const query = useQuery<MeResult>({
    queryKey: AUTH_SESSION_KEY,
    queryFn: fetchMe,
    // Identity changes only on sign-in/out, both of which reload the page.
    staleTime: 5 * 60_000,
    // A failing probe must not retry-storm a deployment that has no auth
    // routes mounted at all; one honest attempt is enough.
    retry: false,
  });

  const session = useMemo(() => {
    const data = query.data;
    return data?.authenticated ? new AuthSession(data) : null;
  }, [query.data]);

  const can = useCallback(
    (permission: ConsolePermission) => session?.can(permission) ?? false,
    [session],
  );

  const refresh = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: AUTH_SESSION_KEY });
  }, [qc]);

  return {
    session,
    // Treat an unreachable/absent `/me` as "auth is off": a deployment running
    // an older API must keep working on its static key exactly as it does now.
    authEnabled: query.data?.auth_enabled ?? false,
    isAnonymous: query.data?.authenticated === false,
    loading: query.isLoading,
    error: query.error instanceof Error ? query.error : null,
    can,
    refresh,
  };
}
