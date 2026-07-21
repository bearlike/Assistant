import type { ReactNode } from "react"
import { Loader2 } from "lucide-react"

import { Button } from "./button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "./dialog"

/**
 * ConfirmDialog — THE two-step confirm for a destructive action.
 *
 * A shadcn-style vendored primitive composing `<Dialog>`: title + description,
 * a ghost cancel and a danger-toned primary that flips to a spinner while the
 * mutation is in flight. Five hand-written copies of exactly this markup used
 * to live in `ApiKeysView`, `GitCredentialsView`, `ProjectCard`,
 * `WorktreesPanel` and `TriggersPane`; `InputBar` (the force-remove-worktree
 * confirm) is the sixth consumer, and a hand-written seventh is a review reject.
 *
 * `open` is derived from the target (`target !== null`) at the call site, so a
 * closed dialog still renders its (empty) description — keep call-site copy
 * null-safe. Dismissal (Esc, overlay click, Cancel) all route to `onCancel`.
 */
export interface ConfirmDialogProps {
  open: boolean
  /** Dialog heading — also its accessible name (Radix `aria-labelledby`). */
  title: string
  description: ReactNode
  /** Danger button label at rest. */
  confirmLabel: string
  /** Danger button label while `pending` (spinner replaces `confirmIcon`). */
  pendingLabel?: string
  /** Ghost button label. Defaults to "Cancel". */
  cancelLabel?: string
  /** Resting icon on the danger button, e.g. `<Trash2 />`. */
  confirmIcon?: ReactNode
  /** Mutation in flight: both buttons disable, the danger one spins. */
  pending?: boolean
  onConfirm: () => void
  onCancel: () => void
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel,
  pendingLabel,
  cancelLabel = "Cancel",
  confirmIcon,
  pending = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) onCancel()
      }}
    >
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
          <DialogDescription>{description}</DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" size="md" onClick={onCancel} disabled={pending}>
            {cancelLabel}
          </Button>
          <Button
            variant="neutral"
            tone="danger"
            size="md"
            onClick={onConfirm}
            disabled={pending}
            leadingIcon={
              pending ? <Loader2 className="w-4 h-4 animate-spin" /> : confirmIcon
            }
          >
            {pending && pendingLabel ? pendingLabel : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
