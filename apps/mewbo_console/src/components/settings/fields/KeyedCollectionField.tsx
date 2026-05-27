/**
 * KeyedCollectionField — an RJSF custom field for `dict[str, X]` config maps.
 *
 * Covers the four keyed-collection settings whose schema is an `object` with
 * `additionalProperties`: `projects`, `model_context_windows`, `channels`, and
 * `lsp.servers`. RJSF's default `additionalProperties` UI renders each entry as
 * a nested object whose headline is the VALUE schema's title (so a collapsed
 * `projects` entry confusingly reads "ProjectConfig"); this field instead makes
 * the KEY the headline via an editable rename input, and picks a value renderer
 * from `schema.additionalProperties`:
 *
 *   - `$ref`            → a small subform of the resolved model's scalar props
 *   - `type: integer|number` → a number input
 *   - object / freeform → the `JsonValueEditor` JSON textarea
 *
 * Body-only pattern (mirrors `RepositoriesField` / `RecordListField`): the
 * wrapping FieldTemplate renders this field's own label + help, so we render
 * ONLY the body — the per-entry cards plus an "Add" dialog. Every mutation
 * rebuilds the object immutably (preserving insertion order) and calls
 * `onChange`; the section footer's Save persists — this field never autosaves.
 */
import { useState } from "react";
import type { FieldProps, RJSFSchema } from "@rjsf/utils";
import { Plus, Trash2 } from "lucide-react";

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
import { Switch } from "../../ui/switch";
import { FieldHelp } from "./FieldHelp";
import { FieldLabel } from "./FieldLabel";
import { JsonValueEditor } from "./JsonValueEditor";
import { helpCls, inputBase } from "../styles";

type Obj = Record<string, unknown>;

/** Narrow an unknown value to a plain (non-array, non-null) object. */
function isPlainObject(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

type ValueKind = "number" | "json" | "subform";

/** Resolve a local `#/$defs/<Name>` `$ref` against the form's root schema. */
function resolveRef(
  ref: string | undefined,
  rootSchema: RJSFSchema | undefined
): RJSFSchema | undefined {
  if (!ref) return undefined;
  const name = ref.replace(/^#\/\$defs\//, "");
  const defs = (rootSchema?.$defs ?? rootSchema?.definitions) as
    | Record<string, RJSFSchema>
    | undefined;
  return defs?.[name];
}

/**
 * Decide, once, how to render each entry's value from `additionalProperties`:
 *   $ref → subform; integer/number → number; everything else → JSON editor.
 */
function selectValueKind(ap: RJSFSchema | undefined): {
  kind: ValueKind;
  def?: RJSFSchema;
} {
  if (!ap || typeof ap !== "object") return { kind: "json" };
  if (typeof ap.$ref === "string") return { kind: "subform" };
  if (ap.type === "integer" || ap.type === "number") return { kind: "number" };
  return { kind: "json" };
}

export function KeyedCollectionField(props: FieldProps<Record<string, unknown>>) {
  const { formData, onChange, disabled, readonly, idSchema, schema, registry } =
    props;
  const entries: Obj = isPlainObject(formData) ? formData : {};
  const baseId = idSchema?.$id ?? "keyedCollection";
  const editable = !disabled && !readonly;

  const ap = schema.additionalProperties as RJSFSchema | undefined;
  const { kind } = selectValueKind(ap);
  // For the subform case, resolve the referenced model def's scalar properties.
  const subDef =
    kind === "subform"
      ? resolveRef(ap?.$ref as string | undefined, registry?.rootSchema)
      : undefined;
  const subProps = (subDef?.properties ?? {}) as Record<string, RJSFSchema>;

  const [addOpen, setAddOpen] = useState(false);

  /** The only mutation seam. */
  const commit = (next: Obj) => onChange(next);

  /** Default value for a newly-added entry. */
  const defaultValue = (): unknown => (kind === "number" ? 0 : {});

  /** Rebuild the object preserving order, swapping `oldKey` → `newKey`. */
  const renameKey = (oldKey: string, newKey: string) => {
    const next: Obj = {};
    for (const [k, v] of Object.entries(entries)) {
      next[k === oldKey ? newKey : k] = v;
    }
    commit(next);
  };

  const removeKey = (key: string) => {
    const next: Obj = {};
    for (const [k, v] of Object.entries(entries)) {
      if (k !== key) next[k] = v;
    }
    commit(next);
  };

  const keyList = Object.keys(entries);
  const labelText = kind === "subform" ? "Name" : "Key";

  return (
    <div className="space-y-3">
      {keyList.length === 0 ? (
        <p className={helpCls}>No entries configured.</p>
      ) : (
        <ul className="space-y-3">
          {Object.entries(entries).map(([key, value]) => {
            const rowId = `${baseId}_${key}`;
            return (
              <li
                key={key}
                className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 space-y-3"
              >
                {/* Headline: the KEY, editable via an uncontrolled rename input. */}
                <div className="flex items-end gap-2">
                  <div className="flex-1 space-y-1">
                    <FieldLabel htmlFor={`${rowId}_key`}>{labelText}</FieldLabel>
                    <input
                      id={`${rowId}_key`}
                      type="text"
                      className={inputBase}
                      // Uncontrolled + `key={key}` so the input re-mounts when the
                      // key changes (so its draft never goes stale). Commit on
                      // blur/Enter; revert when the new name is empty/dup/unchanged.
                      key={key}
                      defaultValue={key}
                      disabled={!editable}
                      aria-label={`${labelText} for ${key}`}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") {
                          e.preventDefault();
                          (e.target as HTMLInputElement).blur();
                        }
                      }}
                      onBlur={(e) => {
                        const next = e.target.value.trim();
                        if (
                          next === "" ||
                          next === key ||
                          (next !== key && next in entries)
                        ) {
                          e.target.value = key; // revert
                          return;
                        }
                        renameKey(key, next);
                      }}
                    />
                  </div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    tone="danger"
                    iconOnly
                    disabled={!editable}
                    onClick={() => removeKey(key)}
                    aria-label={`Remove ${key}`}
                    title={`Remove ${key}`}
                    leadingIcon={<Trash2 className="w-4 h-4" />}
                  />
                </div>

                {/* Value renderer, chosen once from additionalProperties. */}
                {kind === "number" && (
                  <input
                    id={`${rowId}_value`}
                    type="number"
                    className={inputBase}
                    value={value == null ? "" : String(value)}
                    disabled={!editable}
                    aria-label={`Value for ${key}`}
                    onChange={(e) =>
                      commit({
                        ...entries,
                        [key]: e.target.value === "" ? 0 : Number(e.target.value),
                      })
                    }
                  />
                )}

                {kind === "json" && (
                  <JsonValueEditor
                    id={`${rowId}_value`}
                    value={value}
                    disabled={!editable}
                    onChange={(nv) => commit({ ...entries, [key]: nv })}
                  />
                )}

                {kind === "subform" && (
                  <Subform
                    rowId={rowId}
                    properties={subProps}
                    value={isPlainObject(value) ? value : {}}
                    disabled={!editable}
                    onChange={(nv) => commit({ ...entries, [key]: nv })}
                  />
                )}
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
        onClick={() => setAddOpen(true)}
        leadingIcon={<Plus className="w-4 h-4" />}
      >
        Add entry
      </Button>

      <AddEntryDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        labelText={labelText}
        existing={entries}
        onCreate={(name) => {
          commit({ ...entries, [name]: defaultValue() });
          setAddOpen(false);
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Subform — renders the resolved $ref model's scalar properties generically.
// ---------------------------------------------------------------------------

function Subform({
  rowId,
  properties,
  value,
  disabled,
  onChange,
}: {
  rowId: string;
  properties: Record<string, RJSFSchema>;
  value: Obj;
  disabled?: boolean;
  onChange: (next: Obj) => void;
}) {
  return (
    <div className="space-y-3">
      {Object.entries(properties).map(([propKey, propSchema]) => {
        const id = `${rowId}_${propKey}`;
        return (
          <div key={propKey} className="space-y-1">
            <FieldLabel htmlFor={id}>{propSchema.title ?? propKey}</FieldLabel>
            {renderScalar(
              id,
              propSchema,
              value[propKey],
              (nv) => onChange({ ...value, [propKey]: nv }),
              disabled
            )}
            <FieldHelp text={propSchema.description} id={`${id}_help`} />
          </div>
        );
      })}
    </div>
  );
}

/** Render one scalar control typed by its prop schema (DRY, used per-prop). */
function renderScalar(
  id: string,
  propSchema: RJSFSchema,
  val: unknown,
  onChange: (v: unknown) => void,
  disabled?: boolean
) {
  const type = propSchema.type;
  if (type === "boolean") {
    return (
      <div>
        <Switch
          id={id}
          checked={Boolean(val)}
          disabled={disabled}
          onCheckedChange={(checked) => onChange(checked)}
        />
      </div>
    );
  }
  if (type === "integer" || type === "number") {
    return (
      <input
        id={id}
        type="number"
        className={inputBase}
        value={val == null ? "" : String(val)}
        disabled={disabled}
        onChange={(e) =>
          onChange(e.target.value === "" ? null : Number(e.target.value))
        }
      />
    );
  }
  // string (and any other scalar) → text input
  return (
    <input
      id={id}
      type="text"
      className={inputBase}
      value={val == null ? "" : String(val)}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

// ---------------------------------------------------------------------------
// Add-entry dialog — name input + Create (validate non-empty + not duplicate).
// ---------------------------------------------------------------------------

function AddEntryDialog({
  open,
  onOpenChange,
  labelText,
  existing,
  onCreate,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  labelText: string;
  existing: Obj;
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
            <p role="alert" className="text-[hsl(var(--destructive))] text-xs">
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
