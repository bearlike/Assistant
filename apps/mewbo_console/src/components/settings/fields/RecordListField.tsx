/**
 * RecordListField — an RJSF custom field for the four `HooksConfig` arrays.
 *
 * Each entry is a `HookEntry` object:
 *   { type: "command" | "http", command, url, headers, matcher, timeout }
 *
 * Renders one editable card per entry (instead of RJSF's default array +
 * nested-object templates) with a command/http type-switch that conditionally
 * shows the relevant fields:
 *   - command → Command (text), Matcher (text), Timeout (number)
 *   - http    → URL (text, required), Headers (JSON map), Matcher, Timeout
 *
 * Switching the type keeps existing field values (it only toggles visibility),
 * so a user can flip back and forth without losing data. Every mutation builds
 * the next array immutably and calls `onChange(next)`; the section footer's Save
 * persists — this field never autosaves.
 *
 * Body-only pattern (mirrors `RepositoriesField`): the wrapping FieldTemplate
 * already renders this field's own label + help, so we render ONLY the body —
 * the cards plus an "Add hook" button. Per-property labels/help inside each card
 * come from `FieldLabel` / `FieldHelp`, pulling title/description out of
 * `schema.items.properties[<key>]`.
 */
import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { FieldProps, RJSFSchema } from "@rjsf/utils";
import { Plus, Trash2 } from "lucide-react";

import { Button } from "../../ui/button";
import { FieldHelp } from "./FieldHelp";
import { FieldLabel } from "./FieldLabel";
import { helpCls, inputBase } from "../styles";

/** A single hook entry — loosely typed; the backend owns the strict schema. */
type HookEntry = Record<string, unknown>;

const HOOK_TYPES = ["command", "http"] as const;

/** A fresh command-type entry (the Add-hook default). */
function emptyEntry(): HookEntry {
  return {
    type: "command",
    command: "",
    url: "",
    headers: {},
    matcher: null,
    timeout: 30,
  };
}

/** Pull a per-property sub-schema (for title/description) from the item schema. */
function propSchema(
  schema: RJSFSchema | undefined,
  key: string
): RJSFSchema | undefined {
  const items = schema?.items as RJSFSchema | undefined;
  const props = items?.properties as Record<string, RJSFSchema> | undefined;
  return props?.[key];
}

export function RecordListField(props: FieldProps<Array<HookEntry>>) {
  const { formData, onChange, disabled, readonly, idSchema, schema } = props;
  const items = Array.isArray(formData) ? formData : [];
  const baseId = idSchema?.$id ?? "hooks";
  const editable = !disabled && !readonly;

  // Per-property metadata (title/description), resolved once from the schema.
  const meta = useMemo(
    () => ({
      type: propSchema(schema, "type"),
      command: propSchema(schema, "command"),
      url: propSchema(schema, "url"),
      headers: propSchema(schema, "headers"),
      matcher: propSchema(schema, "matcher"),
      timeout: propSchema(schema, "timeout"),
    }),
    [schema]
  );

  /** Immutably merge `patch` into the entry at `index`, then commit. */
  const updateItem = (index: number, patch: HookEntry) => {
    const next = items.map((it, i) => (i === index ? { ...it, ...patch } : it));
    onChange(next);
  };

  const removeItem = (index: number) => {
    onChange(items.filter((_, i) => i !== index));
  };

  const addItem = () => {
    onChange([...items, emptyEntry()]);
  };

  return (
    <div className="space-y-3">
      {items.length === 0 ? (
        <p className={helpCls}>No hooks configured.</p>
      ) : (
        <ul className="space-y-3">
          {items.map((item, index) => {
            const rowId = `${baseId}_${index}`;
            const isHttp = item.type === "http";
            const type: (typeof HOOK_TYPES)[number] = isHttp
              ? "http"
              : "command";

            return (
              <li
                key={rowId}
                className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 space-y-3"
              >
                {/* Header: type switch + remove */}
                <div className="flex items-center gap-2">
                  <select
                    id={`${rowId}_type`}
                    className={inputBase}
                    value={type}
                    disabled={!editable}
                    aria-label={meta.type?.title ?? "Hook type"}
                    onChange={(e) =>
                      updateItem(index, { type: e.target.value })
                    }
                  >
                    {HOOK_TYPES.map((t) => (
                      <option key={t} value={t}>
                        {t}
                      </option>
                    ))}
                  </select>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    tone="danger"
                    iconOnly
                    disabled={!editable}
                    onClick={() => removeItem(index)}
                    aria-label="Remove hook"
                    title="Remove hook"
                    leadingIcon={<Trash2 className="w-4 h-4" />}
                  />
                </div>

                {/* command-only: Command */}
                {!isHttp && (
                  <Field
                    id={`${rowId}_command`}
                    label={meta.command?.title ?? "Command"}
                    help={meta.command?.description}
                  >
                    <input
                      id={`${rowId}_command`}
                      type="text"
                      className={inputBase}
                      value={String(item.command ?? "")}
                      disabled={!editable}
                      onChange={(e) =>
                        updateItem(index, { command: e.target.value })
                      }
                    />
                  </Field>
                )}

                {/* http-only: URL (required) */}
                {isHttp && (
                  <Field
                    id={`${rowId}_url`}
                    label={meta.url?.title ?? "URL"}
                    help={meta.url?.description}
                    required
                  >
                    <input
                      id={`${rowId}_url`}
                      type="text"
                      className={inputBase}
                      value={String(item.url ?? "")}
                      disabled={!editable}
                      required
                      onChange={(e) =>
                        updateItem(index, { url: e.target.value })
                      }
                    />
                  </Field>
                )}

                {/* http-only: Headers (JSON map) */}
                {isHttp && (
                  <HeadersField
                    id={`${rowId}_headers`}
                    label={meta.headers?.title ?? "Headers"}
                    help={meta.headers?.description}
                    value={
                      (item.headers as Record<string, unknown> | undefined) ??
                      {}
                    }
                    disabled={!editable}
                    onChange={(headers) => updateItem(index, { headers })}
                  />
                )}

                {/* both: Matcher */}
                <Field
                  id={`${rowId}_matcher`}
                  label={meta.matcher?.title ?? "Matcher"}
                  help={meta.matcher?.description}
                >
                  <input
                    id={`${rowId}_matcher`}
                    type="text"
                    className={inputBase}
                    value={item.matcher == null ? "" : String(item.matcher)}
                    disabled={!editable}
                    onChange={(e) =>
                      updateItem(index, {
                        matcher: e.target.value === "" ? null : e.target.value,
                      })
                    }
                  />
                </Field>

                {/* both: Timeout */}
                <Field
                  id={`${rowId}_timeout`}
                  label={meta.timeout?.title ?? "Timeout"}
                  help={meta.timeout?.description}
                >
                  <input
                    id={`${rowId}_timeout`}
                    type="number"
                    className={inputBase}
                    value={
                      item.timeout == null ? "" : String(item.timeout)
                    }
                    disabled={!editable}
                    onChange={(e) =>
                      updateItem(index, {
                        timeout:
                          e.target.value === "" ? null : Number(e.target.value),
                      })
                    }
                  />
                </Field>
              </li>
            );
          })}
        </ul>
      )}

      <Button
        type="button"
        variant="ghost"
        size="sm"
        disabled={!editable}
        onClick={addItem}
        leadingIcon={<Plus className="w-4 h-4" />}
      >
        Add hook
      </Button>
    </div>
  );
}

/** A labelled control row inside a card: FieldLabel + body + FieldHelp. */
function Field({
  id,
  label,
  help,
  required,
  children,
}: {
  id: string;
  label: string;
  help?: string;
  required?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="space-y-1">
      {/* The required marker sits OUTSIDE the <label> so the input's accessible
          name stays exactly `label`; the input's own `required` attribute
          carries the semantics for assistive tech. */}
      <div className="flex items-center gap-0.5">
        <FieldLabel htmlFor={id}>{label}</FieldLabel>
        {required ? (
          <span aria-hidden className="text-[hsl(var(--destructive))]">
            *
          </span>
        ) : null}
      </div>
      {children}
      <FieldHelp text={help} id={`${id}_help`} />
    </div>
  );
}

/**
 * Headers editor — a compact JSON textarea over the `Record<string,string>`
 * map. Parsed on change; invalid JSON shows an inline error and is NOT
 * propagated (the last valid value stays in form data) so the form never holds
 * a malformed headers object. Local edit buffer + error live in component
 * state so the user can type freely toward valid JSON.
 */
function HeadersField({
  id,
  label,
  help,
  value,
  disabled,
  onChange,
}: {
  id: string;
  label: string;
  help?: string;
  value: Record<string, unknown>;
  disabled?: boolean;
  onChange: (next: Record<string, unknown>) => void;
}) {
  const serialized = JSON.stringify(value ?? {}, null, 2);
  // Track the raw text + parse error locally; seed from the committed value.
  const [draft, setDraft] = useState<string>(serialized);
  const [error, setError] = useState<string | null>(null);
  // Re-seed the draft when the committed value changes from outside (e.g. a
  // type-switch round-trip) and we're not mid-edit on an invalid buffer.
  useEffect(() => {
    if (error === null) setDraft(serialized);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serialized]);

  const errorId = `${id}_error`;

  const handle = (text: string) => {
    setDraft(text);
    try {
      const parsed = text.trim() === "" ? {} : JSON.parse(text);
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        setError("Headers must be a JSON object.");
        return;
      }
      setError(null);
      onChange(parsed as Record<string, unknown>);
    } catch {
      setError("Invalid JSON.");
    }
  };

  return (
    <div className="space-y-1">
      <FieldLabel htmlFor={id}>{label}</FieldLabel>
      <textarea
        id={id}
        className={inputBase}
        rows={3}
        value={draft}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        onChange={(e) => handle(e.target.value)}
      />
      {error && (
        <p id={errorId} role="alert" className="text-xs text-[hsl(var(--destructive))]">
          {error}
        </p>
      )}
      <FieldHelp text={help} id={`${id}_help`} />
    </div>
  );
}
