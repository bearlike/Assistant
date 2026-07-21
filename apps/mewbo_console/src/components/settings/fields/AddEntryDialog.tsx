/**
 * AddEntryDialog — name-input + Create dialog for a new `KeyedCollectionField`
 * entry. Lifted out of that file: it's a self-contained modal (its own local
 * `name` draft state, its own duplicate/empty validation) with no dependency
 * on the parent's entry map beyond the `existing` keys it validates against.
 */
import { useState } from "react";
import { Plus } from "lucide-react";

import { Button } from "../../ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "../../ui/dialog";
import { Input } from "../../ui/input";
import { FieldLabel } from "./FieldLabel";

export function AddEntryDialog({
  open,
  onOpenChange,
  labelText,
  existing,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  labelText: string;
  existing: Record<string, unknown>;
  onCreate: (name: string) => void;
}) {
  const [name, setName] = useState("");
  const trimmed = name.trim();
  const duplicate = trimmed !== "" && trimmed in existing;
  const valid = trimmed !== "" && !duplicate;

  const reset = (next: boolean) => {
    if (!next) setName("");
    onOpenChange(next);
  };

  const submit = () => {
    if (valid) {
      onCreate(trimmed);
      setName("");
    }
  };

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Add entry</DialogTitle>
          <DialogDescription>
            Enter a unique {labelText.toLowerCase()} for the new entry.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-1">
          <FieldLabel htmlFor="keyed-collection-add-name">{labelText}</FieldLabel>
          <Input
            id="keyed-collection-add-name"
            type="text"
            value={name}
            autoFocus
            aria-label={`${labelText} for new entry`}
            aria-invalid={duplicate ? true : undefined}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              }
            }}
          />
          {duplicate && (
            <p role="alert" className="text-[hsl(var(--destructive-text))] text-xs">
              An entry with this {labelText.toLowerCase()} already exists.
            </p>
          )}
        </div>
        <DialogFooter>
          <Button variant="ghost" size="md" onClick={() => reset(false)}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="md"
            disabled={!valid}
            onClick={submit}
            leadingIcon={<Plus className="w-4 h-4" />}
          >
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
