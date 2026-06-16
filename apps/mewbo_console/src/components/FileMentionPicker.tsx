import { useMemo } from "react";
import { FileText, Paperclip } from "lucide-react";
import { Popover, PopoverAnchor, PopoverContent } from "./ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "./ui/command";

interface FileMentionPickerProps {
  open: boolean;
  /** Text typed after the `@` — used to prefix/substring-filter client-side. */
  query: string;
  /** Project files referenceable by path. */
  files: string[];
  /** Session attachments referenceable by name. */
  attachments: string[];
  side?: "top" | "bottom";
  anchor: React.ReactNode;
  /** Selecting a row hands back the chosen path/name; the parent splices it. */
  onSelect: (path: string) => void;
  onOpenChange: (open: boolean) => void;
}

/** Cap the rendered list so a huge repo doesn't blow up the dropdown. */
const MAX_RESULTS = 50;

function rank(name: string, q: string): boolean {
  return name.toLowerCase().includes(q);
}

/**
 * Caret-anchored autocomplete for `@file` references.
 *
 * A sibling of `CommandPalette` — same Popover + cmdk wrapping, `shouldFilter`
 * off (we own the filter so basename + path both match), keyboard nav + ARIA
 * owned by cmdk. Files and session attachments render as two groups; selecting
 * a row returns the path/name for the parent to splice into the textarea.
 */
export function FileMentionPicker({
  open,
  query,
  files,
  attachments,
  side = "top",
  anchor,
  onSelect,
  onOpenChange,
}: FileMentionPickerProps) {
  const q = query.toLowerCase();
  const filteredFiles = useMemo(
    () => (q ? files.filter((f) => rank(f, q)) : files).slice(0, MAX_RESULTS),
    [files, q],
  );
  const filteredAttachments = useMemo(
    () =>
      (q ? attachments.filter((a) => rank(a, q)) : attachments).slice(
        0,
        MAX_RESULTS,
      ),
    [attachments, q],
  );

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverAnchor asChild>{anchor}</PopoverAnchor>
      <PopoverContent
        side={side}
        align="start"
        sideOffset={6}
        className="w-80 p-0 overflow-hidden"
        onOpenAutoFocus={(e) => e.preventDefault()}
        // Keep textarea focus — selecting a row must not blur the composer.
        onMouseDown={(e) => e.preventDefault()}
      >
        <Command shouldFilter={false}>
          <CommandList>
            <CommandEmpty>No matching file.</CommandEmpty>
            {filteredAttachments.length > 0 && (
              <CommandGroup heading="Attachments">
                {filteredAttachments.map((name) => (
                  <CommandItem
                    key={`att:${name}`}
                    value={`att:${name}`}
                    onSelect={() => onSelect(name)}
                    className="gap-2"
                  >
                    <Paperclip className="opacity-60" />
                    <span className="truncate font-mono text-xs">{name}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
            {filteredFiles.length > 0 && (
              <CommandGroup heading="Files">
                {filteredFiles.map((path) => (
                  <CommandItem
                    key={`file:${path}`}
                    value={`file:${path}`}
                    onSelect={() => onSelect(path)}
                    className="gap-2"
                  >
                    <FileText className="opacity-60" />
                    <span className="truncate font-mono text-xs">{path}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
