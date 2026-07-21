/**
 * VariableReference — the collapsible "Available template variables" table for
 * `SystemInstructionsPane`. Lifted out of that file: it's a self-contained
 * subtree over the `/variables` payload (a table row per variable, each with
 * its own length-adaptive value affordance) with exactly one external effect,
 * dropping a clicked value at the editor's cursor via `onInsert`.
 *
 * See `apps/mewbo_console/CLAUDE.md` → "Settings — faceted shell over RJSF" →
 * "SystemInstructionsPane's variable reference is LENGTH-ADAPTIVE" for why the
 * inline-chips-vs-popover split exists and why it must not collapse into one
 * hardcoded list.
 */
import { useState } from "react";

import type { SystemInstructionsVariable } from "../../../../api/systemInstructions";
import { Button } from "../../../ui/button";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
} from "../../../ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "../../../ui/popover";
import { ErrorAlert } from "../../../ErrorAlert";
import { FieldHelp } from "../../fields/FieldHelp";
import { subsectionTitleCls } from "../../styles";

const WORKED_EXAMPLE = `{% if surface == "android" %}
You are talking to a user on a phone. Keep answers short.
{% endif %}`;

/**
 * Above this many values a list stops being scannable inline and collapses to
 * a count that opens a filterable popover. Eight keeps `origin` (7) and
 * `platform` (3) as chips while `tools` (40+) and `model` (30+) collapse.
 */
const INLINE_VALUE_LIMIT = 8;

const chipCls =
  "font-mono text-[0.9em] bg-[hsl(var(--muted))]/40 hover:bg-[hsl(var(--accent))] rounded px-1 " +
  "text-[hsl(var(--hl-string))] transition-colors";

/**
 * The candidate values for one variable, sized to the list.
 *
 * The two shapes answer different questions. A short list is a fact worth
 * reading at a glance, so it renders as chips. A forty-name list is a thing you
 * search, so it collapses to its count and opens a `<Command>` (cmdk) inside a
 * `<Popover>`: filter box, arrow-key navigation and an empty state, all for
 * free from primitives already vendored here. A hover `<Tooltip>` would have
 * been neither keyboard- nor touch-reachable.
 *
 * Either way a value is a BUTTON: clicking it drops the bare string at the
 * editor's cursor, which is the point of showing the list next to an editor.
 */
function VariableValues({
  variable,
  onInsert,
}: {
  variable: SystemInstructionsVariable;
  onInsert: (value: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const values = variable.values ?? [];
  if (values.length === 0) return null;

  // `closed` means a session's value is always in this list, so a template can
  // branch on it exhaustively. `known` means this is what the deployment has
  // right now, and the note below spells out why that isn't the same promise.
  const label = variable.valuesKind === "closed" ? "Always one of:" : "Available here:";

  const insert = (value: string) => {
    setOpen(false);
    onInsert(value);
  };

  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      <span className="text-[hsl(var(--muted-foreground))]">{label}</span>

      {values.length <= INLINE_VALUE_LIMIT ? (
        values.map((value) => (
          <button
            key={value}
            type="button"
            className={chipCls}
            title={`Insert "${value}" at the cursor`}
            onClick={() => insert(value)}
          >
            {value}
          </button>
        ))
      ) : (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <Button
              variant="ghost"
              size="sm"
              className="h-5 px-1.5 font-medium text-[hsl(var(--hl-string))]"
              aria-label={`Browse the ${values.length} values of ${variable.name}`}
              title={`Browse the ${values.length} values of ${variable.name}`}
            >
              {values.length} values
            </Button>
          </PopoverTrigger>
          <PopoverContent align="start" className="w-64 p-0">
            <Command>
              <CommandInput placeholder={`Filter ${variable.name}…`} />
              <CommandList>
                <CommandEmpty>No matching value.</CommandEmpty>
                {values.map((value) => (
                  <CommandItem
                    key={value}
                    value={value}
                    onSelect={() => insert(value)}
                    className="font-mono text-xs"
                  >
                    {value}
                  </CommandItem>
                ))}
              </CommandList>
            </Command>
          </PopoverContent>
        </Popover>
      )}

      {/* Provenance and its caveat. FieldHelp is the surface's one description
          renderer, and its collapse rule is exactly right here: a note long
          enough to bloat the row collapses to a `?`. It sits OUTSIDE the values
          popover on purpose, as a sibling rather than a popover within one. */}
      <FieldHelp text={variable.valuesNote ?? undefined} />
    </div>
  );
}

export function VariableReference({
  variables,
  loading,
  error,
  onInsert,
}: {
  variables: SystemInstructionsVariable[];
  loading: boolean;
  error: string | null;
  onInsert: (value: string) => void;
}) {
  return (
    <details className="group rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <summary className="cursor-pointer select-none px-4 py-3 hover:bg-[hsl(var(--accent))] transition-colors">
        <span className={subsectionTitleCls}>
          Available template variables
        </span>
      </summary>
      <div className="px-4 pb-4 pt-2 space-y-3 border-t border-[hsl(var(--border))]">
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          Every row below is available directly by name inside{" "}
          <code className="font-mono text-[0.9em] bg-[hsl(var(--muted))]/40 rounded px-1">
            {"{{ }}"}
          </code>{" "}
          and{" "}
          <code className="font-mono text-[0.9em] bg-[hsl(var(--muted))]/40 rounded px-1">
            {"{% %}"}
          </code>
          . An unknown or misspelled variable renders blank instead of breaking the template. For
          example:
        </p>
        <pre className="rounded-md bg-[hsl(var(--code-body))] text-[hsl(var(--code-fg))] border border-[hsl(var(--code-border))] p-3 text-xs font-mono overflow-x-auto">
          {WORKED_EXAMPLE}
        </pre>
        {loading ? (
          <p className="text-xs text-[hsl(var(--muted-foreground))]">Loading variables…</p>
        ) : error ? (
          <ErrorAlert title="Couldn't load variables" error={error} fallback={error} />
        ) : variables.length === 0 ? (
          <p className="text-xs text-[hsl(var(--muted-foreground))]">No variables reported.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-left text-[hsl(var(--muted-foreground))]">
                  <th className="pr-4 py-1 font-medium">Name</th>
                  <th className="pr-4 py-1 font-medium">Type</th>
                  <th className="py-1 font-medium">Description</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[hsl(var(--border))]">
                {variables.map((v) => (
                  <tr key={v.name}>
                    <td className="pr-4 py-1.5 align-top whitespace-nowrap font-mono text-[hsl(var(--hl-variable))]">
                      {v.name}
                    </td>
                    <td className="pr-4 py-1.5 align-top whitespace-nowrap text-[hsl(var(--muted-foreground))]">
                      {v.type}
                    </td>
                    <td className="py-1.5 align-top text-[hsl(var(--foreground))]">
                      {v.description}
                      <VariableValues variable={v} onInsert={onInsert} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </details>
  );
}
