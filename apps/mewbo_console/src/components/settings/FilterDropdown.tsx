/**
 * FilterDropdown — THE "filter by a closed value set" control of the Settings
 * surface, on shadcn `<DropdownMenu>` (Radix owns click-outside, Escape, focus
 * trap and keyboard nav; a native `<select>` re-answers the same question with
 * unthemed chrome).
 *
 * Hoisted out of `TriggersPane`, which built it first for kind/status/session,
 * so `PluginsPane`'s category filter stops answering the same question a second
 * way. `"all"` is the sentinel for "no filter" and is always offered as the
 * first item; `options` should carry only the values actually present in the
 * data, so the menu never offers a filter that would match nothing.
 */
import { ChevronDown } from "lucide-react";

import { Button } from "../ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../ui/dropdown-menu";

export interface FilterOption {
  value: string;
  label: string;
}

export interface FilterDropdownProps {
  /** Rendered as "Label: <active>" — this is the control's accessible name. */
  label: string;
  /** Current value, or `"all"`. */
  value: string;
  options: FilterOption[];
  onChange: (value: string) => void;
}

export function FilterDropdown({ label, value, options, onChange }: FilterDropdownProps) {
  const active = options.find((o) => o.value === value);
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="neutral" size="sm">
          <span className="text-[hsl(var(--muted-foreground))]">{label}:</span>
          <span className="ml-1 font-medium truncate max-w-[160px]">{active?.label ?? "All"}</span>
          <ChevronDown className="w-3 h-3 ml-1 opacity-60" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-48">
        <DropdownMenuItem onSelect={() => onChange("all")}>
          <span className={value === "all" ? "font-medium text-[hsl(var(--foreground))]" : ""}>
            All
          </span>
        </DropdownMenuItem>
        {options
          .filter((o) => o.value !== "all")
          .map((o) => (
            <DropdownMenuItem key={o.value} onSelect={() => onChange(o.value)}>
              <span
                className={value === o.value ? "font-medium text-[hsl(var(--foreground))]" : ""}
              >
                {o.label}
              </span>
            </DropdownMenuItem>
          ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
