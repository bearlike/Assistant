/**
 * The auth wire surface — `/api/auth/{me,login,logout}`.
 *
 * Four things about this seam are load-bearing:
 *
 * 1. **`/me` answers 200 even when auth is DISABLED**, carrying the legacy
 *    full-power principal (`auth_enabled: false`, `subject: "svc:legacy"`,
 *    `roles: ["admin"]`). That is what lets every consumer render
 *    unconditionally instead of branching on a feature flag.
 * 2. **Login is a full-page navigation, not XHR.** `/api/auth/login` answers a
 *    302 to the identity provider; an `fetch` would follow it into a CORS wall
 *    and the user would never see the provider's consent screen.
 * 3. **Logout is a POST**, and it must carry cookies, because clearing the
 *    session cookie is the entire point.
 * 4. **`/me` is also the credential probe.** It is the one call that opts into
 *    `credentials: "include"` before the shared mode is raised; if it comes
 *    back, cookies demonstrably work on this deployment and every later call
 *    can carry them too. See `httpBase.ts` for why that has to be earned
 *    rather than assumed.
 */
import { API_BASE } from "./client";
import { markCredentialsUsable, readJson, withBase as sharedWithBase } from "./httpBase";

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

// ---------------------------------------------------------------------------
// Wire shapes — mirror `auth/routes.py:AuthRoutesController._profile`
// ---------------------------------------------------------------------------

/** A team the caller belongs to, with their role inside it. */
export interface TeamMembership {
  team_id: string;
  team_role: string;
}

/**
 * The avatar precedence chain, in order. Either may be null: `picture_url`
 * only when the identity provider supplied one, `gravatar_url` only when the
 * deployment's avatar policy enables Gravatar and the principal has an email.
 */
export interface AvatarChain {
  picture_url: string | null;
  gravatar_url: string | null;
}

/** How the caller authenticated (`oidc`, `header`, `ldap`, `key`, `legacy`…). */
export interface AuthMethod {
  kind: string;
  issuer: string | null;
}

/** `GET /api/auth/me`, 200 body. */
export interface MeResponse {
  authenticated: true;
  auth_enabled: boolean;
  subject: string;
  kind: string;
  display: string | null;
  email: string | null;
  email_verified: boolean;
  roles: string[];
  teams: TeamMembership[];
  avatar: AvatarChain;
  auth_method: AuthMethod;
  scopes: string[] | null;
  /**
   * The caller's RESOLVED permission ids — built-in and custom role grants
   * unioned server-side.
   *
   * Not derivable from `roles`: a custom role's grants live in the role store,
   * so a client resolving names itself would both duplicate the permission
   * catalog and miss every role an operator defines. This is the field
   * `AuthSession.can()` reads, and the only correct answer to the question.
   *
   * Typed OPTIONAL only for version skew — a console newer than its API. It is
   * always present on a current build; see `can()` for how absence degrades.
   */
  permissions?: string[];
}

/** `GET /api/auth/me`, 401 body — auth is on and the caller is anonymous. */
export interface AnonymousResponse {
  authenticated: false;
  auth_enabled: boolean;
}

export type MeResult = MeResponse | AnonymousResponse;

// ---------------------------------------------------------------------------
// Authenticator discovery — `GET /api/auth/authenticators`
// ---------------------------------------------------------------------------

/** One browser-login authenticator the sign-in screen may offer. */
export interface Authenticator {
  /** Pass back as `/api/auth/login?authenticator=` — the functional field. */
  name: string;
  /** `oidc` or `saml`; drives the label only. */
  kind: string;
  enabled: boolean;
}

export interface AuthenticatorsResponse {
  /** True when a directory (LDAP) authenticator is on, so a form should show. */
  password_login: boolean;
  authenticators: Authenticator[];
}

/**
 * What the login screen may render.
 *
 * The one route on the auth surface that is anonymous by necessity: it is
 * called before a session exists. A deployment with auth off 404s it, which is
 * why an unreachable answer degrades to "nothing to offer" rather than
 * throwing — the login screen is never shown in that case anyway.
 */
export async function fetchAuthenticators(): Promise<AuthenticatorsResponse> {
  const response = await fetch(withBase("/api/auth/authenticators"), {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (!response.ok) {
    return { password_login: false, authenticators: [] };
  }
  const body = (await response.json()) as Partial<AuthenticatorsResponse>;
  return {
    password_login: body.password_login ?? false,
    authenticators: (body.authenticators ?? []).filter((a) => a.enabled),
  };
}

/**
 * Sign in with directory credentials — `POST /api/auth/login/password`.
 *
 * Unlike the provider legs this one IS xhr: it answers 200 with the profile
 * and sets the session cookie inline, so there is no redirect to follow.
 *
 * The server answers a single "Unauthorized" for every failure — wrong user,
 * wrong password, disabled account alike — deliberately, so the form cannot be
 * used to probe which usernames exist. Do not try to say anything more
 * specific than what comes back.
 */
export async function loginWithPassword(
  username: string,
  password: string,
): Promise<MeResponse> {
  const response = await fetch(withBase("/api/auth/login/password"), {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  const profile = await readJson<MeResponse>(response);
  markCredentialsUsable();
  return profile;
}

// ---------------------------------------------------------------------------
// Callback failure slugs — `?auth_error=<slug>` on the console root
// ---------------------------------------------------------------------------

/**
 * The closed set of slugs `AuthRoutesController._error_redirect` can emit.
 * Keep in lockstep with `auth/routes.py`; an unrecognized slug falls back to
 * the generic message rather than rendering a raw slug at the user.
 */
export const AUTH_ERROR_MESSAGES: Record<string, string> = {
  provider_error:
    "Your identity provider reported an error and did not complete the sign-in. Try again, or contact your administrator if it keeps happening.",
  groups_overage:
    "Your account belongs to too many groups for the provider to send them all, so Mewbo could not work out your access. Your administrator needs to enable group claims lookup on the provider.",
  account_disabled: "This account has been disabled. Contact your administrator to restore access.",
  login_failed: "Sign-in could not be completed. Please try again.",
};

/** The human message for an `?auth_error=` slug, with a safe generic default. */
export function authErrorMessage(slug: string): string {
  return AUTH_ERROR_MESSAGES[slug] ?? AUTH_ERROR_MESSAGES.login_failed;
}

// ---------------------------------------------------------------------------
// Calls
// ---------------------------------------------------------------------------

/**
 * Read the caller's profile.
 *
 * A 401 is a normal, expected answer (anonymous against an auth-enabled
 * deployment), so it resolves to the anonymous shape rather than throwing —
 * an unauthenticated visitor is a state to render, not an error to report.
 * Any other non-2xx still throws through `readJson`.
 */
export async function fetchMe(): Promise<MeResult> {
  const response = await fetch(withBase("/api/auth/me"), {
    headers: { Accept: "application/json" },
    credentials: "include",
  });
  if (response.status === 401) {
    const body = (await response.json().catch(() => ({}))) as Partial<AnonymousResponse>;
    return { authenticated: false, auth_enabled: body.auth_enabled ?? true };
  }
  const profile = await readJson<MeResponse>(response);
  // The round trip succeeded WITH credentials, so cookies are usable here.
  markCredentialsUsable();
  return profile;
}

/**
 * Navigate to the identity provider.
 *
 * `return_to` must be a path on this origin — it is where the callback sends
 * the browser once the provider is done. Defaults to the current location so
 * signing in from a deep link lands back on that deep link.
 */
export function startLogin(authenticator?: string, returnTo?: string): void {
  const params = new URLSearchParams();
  if (authenticator) params.set("authenticator", authenticator);
  params.set("return_to", returnTo ?? `${window.location.pathname}${window.location.search}`);
  window.location.assign(withBase(`/api/auth/login?${params.toString()}`));
}

/**
 * Clear the session cookie.
 *
 * The route also supports `?idp=1`, which answers a 302 through the provider's
 * end-session endpoint so the caller is signed out THERE too. The console does
 * not use it: the route is POST-only, and a cross-origin 302 cannot be followed
 * out of a `fetch` into a real navigation. Wiring it would mean submitting a
 * hidden form to get a navigating POST — worth doing only if single-logout
 * becomes a requirement.
 */
export async function logout(): Promise<void> {
  await fetch(withBase("/api/auth/logout"), {
    method: "POST",
    credentials: "include",
  });
}
