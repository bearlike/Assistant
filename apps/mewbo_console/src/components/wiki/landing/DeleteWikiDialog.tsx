/**
 * Delete-confirm dialog for a wiki project, opened from `ProjectCard`'s
 * trash affordance. Controlled by `slug` — non-null = open with that slug
 * pending, `null` = closed.
 */
import { Loader2, Trash2, TriangleAlert } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

export function DeleteWikiDialog({
  slug,
  pending,
  onOpenChange,
  onConfirm,
}: {
  /** The slug pending deletion, or `null` when the dialog is closed. */
  slug: string | null;
  pending: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <Dialog open={slug !== null} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <TriangleAlert className="h-4 w-4 text-[hsl(var(--destructive))]" />
            Delete this wiki?
          </DialogTitle>
          <DialogDescription>
            Would you like to permanently delete this indexed repository?
            {slug && (
              <span className="block mt-2 font-mono text-xs text-[hsl(var(--foreground))] bg-[hsl(var(--muted))]/60 rounded px-1.5 py-0.5 w-fit">
                {slug}
              </span>
            )}
            <span className="block mt-3 text-xs">
              The wiki pages, diagrams, and Q&amp;A history for this
              repository will be removed. This cannot be undone.
            </span>
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => onOpenChange(false)}
            disabled={pending}
          >
            Cancel
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            disabled={pending}
            onClick={onConfirm}
            className="bg-[hsl(var(--destructive))] hover:bg-[hsl(var(--destructive))]/90 text-[hsl(var(--destructive-foreground))]"
            leadingIcon={
              pending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Trash2 className="h-3.5 w-3.5" />
              )
            }
          >
            {pending ? "Deleting…" : "Delete wiki"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
