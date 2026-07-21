import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { isSessionTerminatedError } from "../../api/triggers";
import { TRIGGERS_ROOT, useTerminateSession } from "../../hooks/useTriggers";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../ui/dialog";

/**
 * Destructive confirm for permanent session termination.
 * Controlled by the caller (the SessionHeader overflow menu opens it). Owns the
 * `useTerminateSession` mutation so the menu stays presentational. An
 * already-terminated session (410) is treated as success — the end state the
 * user asked for is already true.
 */
export function TerminateSessionDialog({
  sessionId,
  open,
  onOpenChange,
}: {
  sessionId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const terminate = useTerminateSession();
  const qc = useQueryClient();

  const handleConfirm = () => {
    terminate.mutate(sessionId, {
      onSuccess: (res) => {
        const n = res.cancelled_triggers ?? 0;
        toast.success(
          n > 0
            ? `Session terminated — ${n} armed trigger${n === 1 ? "" : "s"} cancelled.`
            : "Session terminated.",
        );
        onOpenChange(false);
      },
      onError: (err) => {
        if (isSessionTerminatedError(err)) {
          // Already terminated — the desired end state, not an error. The
          // success path's invalidation (useTriggers.ts useTerminateSession)
          // doesn't run on this 410, so mirror it here or the session row
          // lingers stale.
          void qc.invalidateQueries({ queryKey: ["sessions"] });
          void qc.invalidateQueries({ queryKey: TRIGGERS_ROOT });
          onOpenChange(false);
          return;
        }
        toast.error(
          `Couldn't terminate session — ${err instanceof Error ? err.message : String(err)}`,
        );
      },
    });
  };

  return (
    <Dialog open={open} onOpenChange={(next) => !terminate.isPending && onOpenChange(next)}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Terminate this session?</DialogTitle>
          <DialogDescription>
            This is irreversible. The session can never run again, and every armed
            trigger it owns is cancelled. Its transcript stays readable.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={terminate.isPending}
          >
            Keep session
          </Button>
          <Button
            variant="primary"
            size="sm"
            tone="danger"
            onClick={handleConfirm}
            disabled={terminate.isPending}
          >
            {terminate.isPending ? "Terminating…" : "Terminate session"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
