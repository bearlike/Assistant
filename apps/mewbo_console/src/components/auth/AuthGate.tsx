/**
 * `AuthGate` — decides whether the console renders, or the login screen does.
 *
 * ## Why this does not simply block on the `/me` query
 *
 * With auth DISABLED the console must behave exactly as it did before this
 * layer existed. Holding every route behind a spinner until `/me` answers
 * would change that for the deployments that need none of it, so the default
 * while the probe is in flight is to render the app.
 *
 * The cost of that default is a flash of app chrome before the login screen on
 * an auth-enabled deployment. `AUTH_ENABLED_HINT` buys it back: once a `/me`
 * has reported auth is on, the flag is remembered and subsequent loads wait for
 * the probe instead of flashing. A deployment with auth off never writes the
 * flag, so it never waits — the fast path stays the unauthenticated one, which
 * is the right way round.
 *
 * The hint is a rendering optimization and nothing more. It cannot grant
 * access (the API decides that) and a stale one costs at most one flash.
 */
import { useCallback, useEffect, useState, type ReactNode } from "react";

import { useAuthSession } from "@/hooks/useAuthSession";
import { LoginScreen } from "./LoginScreen";

const AUTH_ENABLED_HINT = "mewbo:auth-enabled";

function readHint(): boolean {
  try {
    return window.localStorage.getItem(AUTH_ENABLED_HINT) === "1";
  } catch {
    return false;
  }
}

function writeHint(enabled: boolean): void {
  try {
    if (enabled) window.localStorage.setItem(AUTH_ENABLED_HINT, "1");
    else window.localStorage.removeItem(AUTH_ENABLED_HINT);
  } catch {
    // Persistence is best-effort (private browsing, quota).
  }
}

/** Strip `?auth_error=` from the URL without adding a history entry. */
function clearAuthError(): void {
  const url = new URL(window.location.href);
  url.searchParams.delete("auth_error");
  window.history.replaceState({}, "", url.toString());
}

export interface AuthGateProps {
  children: ReactNode;
}

export function AuthGate({ children }: AuthGateProps) {
  const { authEnabled, isAnonymous, loading } = useAuthSession();

  // Read once, then owned by state: dismissing has to both strip the slug from
  // the URL and re-render, and the URL alone can do neither.
  const [authError, setAuthError] = useState<string | null>(
    () => new URLSearchParams(window.location.search).get("auth_error"),
  );

  const dismissAuthError = useCallback(() => {
    clearAuthError();
    setAuthError(null);
  }, []);

  useEffect(() => {
    if (!loading) writeHint(authEnabled);
  }, [loading, authEnabled]);

  if (isAnonymous) {
    return <LoginScreen authError={authError} onDismissError={dismissAuthError} />;
  }

  // Only a deployment already known to require sign-in pays the wait.
  if (loading && readHint()) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-[hsl(var(--rail-bg))]">
        <span className="text-sm text-[hsl(var(--muted-foreground))]">Loading…</span>
      </div>
    );
  }

  return <>{children}</>;
}
