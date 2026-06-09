import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useLocation } from "wouter";
import { toast } from "sonner";

import { recoverSession } from "../api/client";
import type { RecoverResponse } from "../api/contracts";
import { buildHref } from "../components/wiki/router";

export interface RecoverVars {
  sessionId: string;
  /** "continue" re-drives the SAME session (context intact); "retry" restarts the last turn. */
  action: "retry" | "continue";
  fromTs?: string;
  editedText?: string;
  model?: string;
  /**
   * Called for a generic (non-wiki) recovery once accepted — the session is
   * re-driving on its own stream, so the caller refreshes / re-opens it.
   * Skipped on a wiki-indexing dispatch (the hook navigates to the indexing
   * screen instead).
   */
  onGeneric?: (res: RecoverResponse) => void;
}

/**
 * Shared recovery mutation for any recoverable session. POSTs to
 * ``/api/sessions/<id>/recover`` and routes by response shape:
 *   - wiki-indexing dispatch (``job_id``, no ``run_id``) → navigate to the
 *     wiki indexing screen so the user watches the resumed index.
 *   - generic (``run_id``) → invalidate the session lists + run the caller's
 *     ``onGeneric`` so the session view refreshes in place.
 *
 * One hook, one fetch path — callers never duplicate the recover call.
 */
export function useRecoverSession() {
  const qc = useQueryClient();
  const [, navigate] = useLocation();
  return useMutation({
    mutationFn: (vars: RecoverVars) =>
      recoverSession(
        vars.sessionId,
        vars.action,
        vars.fromTs,
        vars.editedText,
        vars.model,
      ),
    onSuccess: (res, vars) => {
      if (res.job_id) {
        navigate(buildHref({ kind: "indexing", jobId: res.job_id, slug: res.slug }));
        return;
      }
      void qc.invalidateQueries({ queryKey: ["sessions"] });
      vars.onGeneric?.(res);
    },
    // A failed recover POST (400 "already running" / 404 / 409) must surface —
    // the mutation settles so the buttons re-enable, the toast tells why.
    onError: (err) => {
      toast.error(
        `Recovery failed — ${err instanceof Error ? err.message : String(err)}`,
      );
    },
  });
}
