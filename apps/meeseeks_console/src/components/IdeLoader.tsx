import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, AlertCircle, X } from "lucide-react";
import {
  createIde,
  getIde,
  stopIde,
  IdeApiError,
  IdeInstance,
  IdeStatus,
} from "../api/ide";
import { Button } from "./ui/button";

type LoaderPhase = "creating" | "polling" | "redirecting" | "error";

interface IdeLoaderProps {
  sessionId: string;
}

const STATUS_LABELS: Record<IdeStatus, string> = {
  pending: "Pulling image…",
  starting: "Starting code-server…",
  ready: "Redirecting…",
};

const POLL_INTERVAL_MS = 1000;
const POLL_TIMEOUT_MS = 60_000;

/**
 * Loader route at `/ide-loader/:sessionId`.
 *
 * State machine:
 *   creating  -> POST /api/sessions/<sid>/ide
 *   polling   -> GET /api/sessions/<sid>/ide every 1s up to 60s
 *   redirecting -> hidden form POST to /ide/<sid>/login with password
 *   error     -> show message + Retry
 */
export function IdeLoader({ sessionId }: IdeLoaderProps) {
  const [phase, setPhase] = useState<LoaderPhase>("creating");
  const [status, setStatus] = useState<IdeStatus>("pending");
  const [errorMessage, setErrorMessage] = useState<string>("");
  const [attempt, setAttempt] = useState(0);
  const passwordRef = useRef<string | null>(null);
  const redirectedRef = useRef(false);

  const submitLoginForm = useCallback(
    (password: string) => {
      if (redirectedRef.current) return;
      redirectedRef.current = true;
      const form = document.createElement("form");
      form.method = "POST";
      form.action = `/ide/${sessionId}/login`;
      form.style.display = "none";

      const pwInput = document.createElement("input");
      pwInput.type = "hidden";
      pwInput.name = "password";
      pwInput.value = password;
      form.appendChild(pwInput);

      // code-server uses `base` for the post-login redirect target.
      const baseInput = document.createElement("input");
      baseInput.type = "hidden";
      baseInput.name = "base";
      baseInput.value = `/ide/${sessionId}/`;
      form.appendChild(baseInput);

      document.body.appendChild(form);
      try {
        form.submit();
      } finally {
        // Remove the form even if submit() throws — happy path navigates
        // away and the removal is a no-op, but the element must not leak.
        form.remove();
      }
    },
    [sessionId]
  );

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();
    redirectedRef.current = false;
    passwordRef.current = null;
    setPhase("creating");
    setErrorMessage("");
    setStatus("pending");

    const isAbort = (err: unknown): boolean =>
      err instanceof DOMException && err.name === "AbortError";

    const describe = (err: unknown): string => {
      if (err instanceof IdeApiError) return err.message;
      if (err instanceof Error) return err.message;
      return String(err);
    };

    const run = async () => {
      // Phase 1: create (or reconnect).
      let instance: IdeInstance;
      try {
        instance = await createIde(sessionId, controller.signal);
      } catch (err) {
        if (cancelled || isAbort(err)) return;
        setErrorMessage(describe(err));
        setPhase("error");
        return;
      }
      if (cancelled) return;
      if (!instance.password) {
        setErrorMessage("IDE create response did not include a password.");
        setPhase("error");
        return;
      }
      passwordRef.current = instance.password;
      setStatus(instance.status);

      if (instance.status === "ready") {
        setPhase("redirecting");
        submitLoginForm(instance.password);
        return;
      }

      // Phase 2: poll until ready or timeout.
      setPhase("polling");
      const deadline = Date.now() + POLL_TIMEOUT_MS;
      while (!cancelled) {
        const remaining = deadline - Date.now();
        if (remaining <= 0) {
          setErrorMessage(
            "Timed out waiting for the IDE container to become ready."
          );
          setPhase("error");
          return;
        }
        await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
        if (cancelled) return;
        try {
          const latest = await getIde(sessionId, controller.signal);
          if (cancelled) return;
          if (!latest) {
            setErrorMessage("IDE instance disappeared before becoming ready.");
            setPhase("error");
            return;
          }
          setStatus(latest.status);
          if (latest.status === "ready") {
            const pw = passwordRef.current;
            if (!pw) {
              // Should never happen — POST phase always captures it.
              setErrorMessage("Lost the IDE password mid-flight.");
              setPhase("error");
              return;
            }
            setPhase("redirecting");
            submitLoginForm(pw);
            return;
          }
        } catch (err) {
          if (cancelled || isAbort(err)) return;
          setErrorMessage(describe(err));
          setPhase("error");
          return;
        }
      }
    };

    void run();
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [sessionId, attempt, submitLoginForm]);

  const handleRetry = () => {
    setAttempt((n) => n + 1);
  };

  const handleCancel = async () => {
    try {
      await stopIde(sessionId);
    } catch {
      // Best-effort cancel — ignore errors, still close the tab.
    }
    window.close();
  };

  return (
    <div className="relative min-h-screen flex items-center justify-center bg-[hsl(var(--background))] text-[hsl(var(--foreground))] p-6 overflow-hidden">
      {/* Animated floral background — 15 logo instances rotating at varying speeds */}
      <img
        src="/session-ide-floral-background-animation.svg"
        alt=""
        aria-hidden="true"
        draggable={false}
        className="absolute inset-0 w-full h-full object-cover pointer-events-none select-none"
      />
      <div className="relative w-full max-w-md rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6 shadow-lg">
        <div className="flex items-center gap-3 mb-4">
          <div className="h-8 w-8 rounded-md bg-[hsl(var(--primary))]/10 flex items-center justify-center">
            {phase === "error" ? (
              <AlertCircle className="w-4 h-4 text-red-500" />
            ) : (
              <Loader2 className="w-4 h-4 text-[hsl(var(--primary))] animate-spin" />
            )}
          </div>
          <div className="min-w-0">
            <h1 className="text-sm font-semibold">Meeseeks Web IDE</h1>
            <p className="text-xs text-[hsl(var(--muted-foreground))] font-mono truncate">
              {sessionId}
            </p>
          </div>
        </div>

        {phase !== "error" && (
          <div className="space-y-3">
            <p className="text-sm">{STATUS_LABELS[status]}</p>
            <div className="h-1 w-full rounded-full bg-[hsl(var(--border))] overflow-hidden">
              <div
                className="h-full bg-[hsl(var(--primary))] transition-all duration-500"
                style={{
                  width:
                    status === "pending"
                      ? "33%"
                      : status === "starting"
                      ? "66%"
                      : "100%",
                }}
              />
            </div>
            <Button
              variant="neutral"
              size="md"
              leadingIcon={<X className="w-4 h-4" />}
              onClick={handleCancel}
            >
              Cancel
            </Button>
          </div>
        )}

        {phase === "error" && (
          <div className="space-y-3">
            <p className="text-sm font-medium text-red-500">
              Failed to open Web IDE
            </p>
            <p className="text-xs font-mono text-red-500/80 break-words whitespace-pre-wrap">
              {errorMessage}
            </p>
            <div className="flex gap-2">
              <Button
                variant="neutral"
                size="md"
                tone="info"
                onClick={handleRetry}
              >
                Retry
              </Button>
              <Button
                variant="neutral"
                size="md"
                onClick={handleCancel}
              >
                Close
              </Button>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default IdeLoader;
