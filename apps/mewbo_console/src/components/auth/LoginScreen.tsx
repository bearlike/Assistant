/**
 * The sign-in screen, shown when auth is enabled and nobody is signed in.
 *
 * What it offers comes entirely from `GET /api/auth/authenticators` — the one
 * anonymous route on the auth surface, which exists precisely so a login screen
 * never renders a button that cannot work. Only ENABLED browser authenticators
 * come back, and `password_login` says whether a directory (LDAP) form should
 * show. Nothing here is hardcoded, so configuring a new provider changes this
 * screen with no console change.
 *
 * The two legs are deliberately different transports: a provider sign-in is a
 * full-page navigation (the 302 has to reach the browser), while the password
 * form is xhr (it answers 200 with the profile and sets the cookie inline).
 */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { LogIn } from "lucide-react";

import { BrandMark } from "@/components/BrandMark";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import {
  authErrorMessage,
  fetchAuthenticators,
  loginWithPassword,
  startLogin,
  type Authenticator,
} from "@/api/auth";
import { getErrorMessage } from "@/utils/errors";

const passwordSchema = z.object({
  username: z.string().trim().min(1, "Username is required"),
  password: z.string().min(1, "Password is required"),
});

type PasswordValues = z.infer<typeof passwordSchema>;

/** A provider's human label. `kind` is presentation only. */
function authenticatorLabel(authenticator: Authenticator): string {
  const pretty = authenticator.name
    .replace(/[-_]+/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return `Continue with ${pretty}`;
}

/**
 * Where sign-in should land: the path the visitor is already on, so arriving
 * anonymously at a deep link and signing in returns them to it.
 *
 * `?auth_error=` is stripped because it describes the attempt that failed, not
 * the destination — carrying it through would put a stale failure slug in the
 * address bar of a signed-in console. Read at click time rather than captured
 * at mount so that dismissing the error is reflected here too. Relative by
 * construction; the server hardens `return_to` again on its side.
 */
function returnPath(): string {
  const url = new URL(window.location.href);
  url.searchParams.delete("auth_error");
  return `${url.pathname}${url.search}`;
}

function Banner({ children }: { children: React.ReactNode }) {
  return (
    <div
      role="alert"
      className="mt-6 rounded-md border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 p-3"
    >
      <p className="text-xs text-[hsl(var(--destructive-text))]">{children}</p>
    </div>
  );
}

export interface LoginScreenProps {
  /** The `?auth_error=` slug carried back by a failed callback, if any. */
  authError?: string | null;
  /** Clears the slug from the URL so a retry starts clean. */
  onDismissError?: () => void;
}

export function LoginScreen({ authError, onDismissError }: LoginScreenProps) {
  const [redirecting, setRedirecting] = useState(false);

  const { data, isLoading } = useQuery({
    queryKey: ["auth", "authenticators"],
    queryFn: fetchAuthenticators,
    staleTime: 5 * 60_000,
    retry: false,
  });

  const authenticators = data?.authenticators ?? [];
  const passwordLogin = data?.password_login ?? false;

  const form = useForm<PasswordValues>({
    resolver: zodResolver(passwordSchema),
    defaultValues: { username: "", password: "" },
  });
  const [passwordError, setPasswordError] = useState<string | null>(null);

  const handleProvider = (name?: string) => {
    setRedirecting(true);
    startLogin(name, returnPath());
  };

  const handlePasswordSubmit = form.handleSubmit(async (values) => {
    setPasswordError(null);
    try {
      await loginWithPassword(values.username, values.password);
      // A full reload rather than a client-side transition: every query cached
      // before sign-in belongs to the anonymous session and must be dropped.
      window.location.assign(returnPath());
    } catch (err) {
      setPasswordError(getErrorMessage(err, "Sign-in failed. Check your details and try again."));
    }
  });

  // No provider and no form means the deployment requires sign-in but exposes
  // no way to do it. Say so plainly instead of rendering a dead screen.
  const nothingOffered = !isLoading && authenticators.length === 0 && !passwordLogin;

  return (
    <main className="flex min-h-screen items-center justify-center bg-[hsl(var(--rail-bg))] px-6 py-10">
      <div className="w-full max-w-sm">
        <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-8">
          <div className="flex flex-col items-center text-center">
            <BrandMark size={36} className="text-[hsl(var(--primary))]" />
            <h1 className="mt-4 text-base font-semibold text-[hsl(var(--foreground))]">
              Sign in to Mewbo
            </h1>
            <p className="mt-1.5 text-sm text-[hsl(var(--muted-foreground))]">
              {passwordLogin && authenticators.length === 0
                ? "Use your directory account to continue."
                : "This deployment uses your organization's identity provider."}
            </p>
          </div>

          {authError && (
            <Banner>
              {authErrorMessage(authError)}
              {onDismissError && (
                <button
                  type="button"
                  onClick={onDismissError}
                  className="mt-2 block text-xs font-medium underline underline-offset-2 hover:opacity-80"
                >
                  Dismiss
                </button>
              )}
            </Banner>
          )}

          {nothingOffered && (
            <Banner>
              This deployment requires sign-in, but no sign-in method is configured. An
              administrator needs to enable an identity provider or directory login.
            </Banner>
          )}

          {isLoading && (
            <p className="mt-6 text-center text-sm text-[hsl(var(--muted-foreground))]">
              Loading sign-in options…
            </p>
          )}

          {authenticators.length > 0 && (
            <div className="mt-6 space-y-2">
              {authenticators.map((authenticator) => (
                <Button
                  key={authenticator.name}
                  variant="primary"
                  size="lg"
                  className="w-full"
                  disabled={redirecting}
                  onClick={() => handleProvider(authenticator.name)}
                  leadingIcon={<LogIn className="h-4 w-4" />}
                >
                  {redirecting ? "Redirecting…" : authenticatorLabel(authenticator)}
                </Button>
              ))}
            </div>
          )}

          {authenticators.length > 0 && passwordLogin && (
            <div className="my-6 flex items-center gap-3">
              <span className="h-px flex-1 bg-[hsl(var(--border))]" />
              <span className="text-xs text-[hsl(var(--muted-foreground))]">or</span>
              <span className="h-px flex-1 bg-[hsl(var(--border))]" />
            </div>
          )}

          {passwordLogin && (
            <Form {...form}>
              <form
                onSubmit={handlePasswordSubmit}
                className={authenticators.length > 0 ? "space-y-3" : "mt-6 space-y-3"}
              >
                <FormField
                  control={form.control}
                  name="username"
                  render={({ field }) => (
                    <FormItem className="space-y-1">
                      <FormLabel>Username</FormLabel>
                      <FormControl>
                        <Input {...field} autoComplete="username" autoFocus />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="password"
                  render={({ field }) => (
                    <FormItem className="space-y-1">
                      <FormLabel>Password</FormLabel>
                      <FormControl>
                        <Input {...field} type="password" autoComplete="current-password" />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <Button
                  type="submit"
                  variant="primary"
                  size="lg"
                  className="w-full"
                  disabled={form.formState.isSubmitting}
                >
                  {form.formState.isSubmitting ? "Signing in…" : "Sign in"}
                </Button>
              </form>
            </Form>
          )}

          {/* The server answers one message for every password failure, on
              purpose, so the form cannot be used to probe which usernames
              exist. Show exactly what came back and nothing more specific. */}
          {passwordError && <Banner>{passwordError}</Banner>}
        </div>

        <p className="mt-4 text-center text-xs text-[hsl(var(--muted-foreground))]">
          Trouble signing in? Contact your Mewbo administrator.
        </p>
      </div>
    </main>
  );
}
