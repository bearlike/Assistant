import { useMemo } from "react";
import { Slash, Sparkles } from "lucide-react";
import { Popover, PopoverAnchor, PopoverContent } from "./ui/popover";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandItem,
  CommandList,
} from "./ui/command";
import { CommandSpec } from "../types";
import { SkillSummary } from "../api/contracts";

interface CommandPaletteProps {
  open: boolean;
  query: string;
  commands: CommandSpec[];
  /** User-invocable skills — surfaced as a second group under `/`. */
  skills?: SkillSummary[];
  side?: "top" | "bottom";
  anchor: React.ReactNode;
  onSelect: (cmd: CommandSpec) => void;
  /** Selecting a skill row — the parent inserts `/skill-name ` into the input. */
  onSelectSkill?: (skill: SkillSummary) => void;
  onOpenChange: (open: boolean) => void;
}

/**
 * Autocomplete dropdown for slash `/` — commands AND skills.
 *
 * Wraps shadcn's <Command> (cmdk) inside a <Popover> anchored to the
 * caller-provided element. cmdk owns the keyboard navigation, ARIA, and
 * filter — we just hand it a prefix-filtered list and let it render. Commands
 * (server handlers, run via a dedicated endpoint) and skills (invoked by
 * submitting `/skill-name` as a message) are two visually-distinct groups so
 * the user can tell what a row will do.
 */
export function CommandPalette({
  open,
  query,
  commands,
  skills = [],
  side = "top",
  anchor,
  onSelect,
  onSelectSkill,
  onOpenChange,
}: CommandPaletteProps) {
  const q = query.toLowerCase();
  const filteredCommands = useMemo(() => {
    if (!q) return commands;
    return commands.filter((c) => c.name.startsWith(q));
  }, [commands, q]);
  const filteredSkills = useMemo(() => {
    if (!q) return skills;
    return skills.filter((s) => s.name.toLowerCase().startsWith(q));
  }, [skills, q]);

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
            <CommandEmpty>No matching command or skill.</CommandEmpty>
            {filteredCommands.length > 0 && (
              <CommandGroup heading="Commands">
                {filteredCommands.map((cmd) => (
                  <CommandItem
                    key={`cmd:${cmd.name}`}
                    value={`cmd:${cmd.name}`}
                    onSelect={() => onSelect(cmd)}
                  >
                    <Slash className="opacity-60" />
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
            )}
            {filteredSkills.length > 0 && (
              <CommandGroup heading="Skills">
                {filteredSkills.map((skill) => (
                  <CommandItem
                    key={`skill:${skill.name}`}
                    value={`skill:${skill.name}`}
                    onSelect={() => onSelectSkill?.(skill)}
                  >
                    <Sparkles className="opacity-60 text-[hsl(var(--primary))]" />
                    <div className="flex flex-col gap-0.5">
                      <span className="font-mono text-xs font-semibold">
                        /{skill.name}
                      </span>
                      <span className="text-[11px] text-[hsl(var(--muted-foreground))]">
                        {skill.description}
                      </span>
                    </div>
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
