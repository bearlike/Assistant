import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";

import { forkSession } from "../api/client";
import type { ForkResponse } from "../api/contracts";

export interface ForkVars {
  sessionId: string;
  /** Omitted forks the whole transcript; present branches at that point. */
  fromTs?: string;
  model?: string;
  onSuccess?: (res: ForkResponse) => void;
}

/**
 * Shared fork mutation for both "Branch in new chat" (``fromTs`` given) and
 * "Fork session" (whole transcript). POSTs to
 * ``/api/sessions/<id>/fork`` and invalidates the session list so the new
 * session shows up in the sidebar; a failed fork surfaces a toast instead of
 * being swallowed. One hook, one fetch path — callers never duplicate the
 * fork call or its error handling.
 */
export function useForkSession() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (vars: ForkVars) =>
      forkSession(vars.sessionId, { fromTs: vars.fromTs, model: vars.model }),
    onSuccess: (res, vars) => {
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      vars.onSuccess?.(res);
    },
    onError: (err) => {
      toast.error(
        `Fork failed — ${err instanceof Error ? err.message : String(err)}`,
      );
    },
  });
}
