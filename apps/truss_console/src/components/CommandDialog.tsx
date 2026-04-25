import { Dialog, DialogContent, DialogHeader, DialogTitle } from "./ui/dialog";
import { MarkdownContent } from "./MessageBubble";

interface CommandDialogProps {
  open: boolean;
  title: string;
  body: string;
  onClose: () => void;
}

/**
 * Reusable info modal for ``dialog``-render command results
 * (``/help``, ``/skills``, ``/tokens``). Body is rendered as markdown using
 * the same renderer the chat uses for assistant replies.
 */
export function CommandDialog({
  open,
  title,
  body,
  onClose,
}: CommandDialogProps) {
  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) onClose();
      }}
    >
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{title}</DialogTitle>
        </DialogHeader>
        <div className="text-sm prose prose-sm dark:prose-invert max-w-none">
          <MarkdownContent content={body} />
        </div>
      </DialogContent>
    </Dialog>
  );
}
