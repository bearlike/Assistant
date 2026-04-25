import { useMemo } from "react";
import { Popover, PopoverAnchor, PopoverContent } from "./ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "./ui/command";
import { CommandSpec } from "../types";

interface CommandPaletteProps {
  open: boolean;
  query: string;
  commands: CommandSpec[];
  side?: "top" | "bottom";
  anchor: React.ReactNode;
  onSelect: (cmd: CommandSpec) => void;
  onOpenChange: (open: boolean) => void;
}

/**
 * Autocomplete dropdown for slash commands.
 *
 * Wraps shadcn's <Command> (cmdk) inside a <Popover> anchored to the
 * caller-provided element. cmdk owns the keyboard navigation, ARIA, and
 * filter — we just hand it a prefix-filtered list and let it render.
 */
export function CommandPalette({
  open,
  query,
  commands,
  side = "top",
  anchor,
  onSelect,
  onOpenChange,
}: CommandPaletteProps) {
  const filtered = useMemo(() => {
    const q = query.toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => c.name.startsWith(q));
  }, [commands, query]);

  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverAnchor asChild>{anchor}</PopoverAnchor>
      <PopoverContent
        side={side}
        align="start"
        sideOffset={6}
        className="w-80 p-0 overflow-hidden"
        onOpenAutoFocus={(e) => e.preventDefault()}
      >
        <Command shouldFilter={false}>
          <CommandList>
            <CommandEmpty>No matching command.</CommandEmpty>
            <CommandGroup heading="Commands">
              {filtered.map((cmd) => (
                <CommandItem
                  key={cmd.name}
                  value={cmd.name}
                  onSelect={() => onSelect(cmd)}
                >
                  <div className="flex flex-col gap-0.5">
                    <span className="font-mono text-xs font-semibold">
                      {cmd.usage}
                    </span>
                    <span className="text-[11px] text-[hsl(var(--muted-foreground))]">
                      {cmd.description}
                    </span>
                  </div>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
