/**
 * ArrayFieldTemplate — the console-themed renderer for EVERY RJSF array.
 *
 * RJSF's default array template ships its own Add/Remove/Move toolbar buttons.
 * Under the faceted Settings shell those defaults render as invisible `0×0px`
 * controls (no themed CSS), so the array is effectively un-editable. This
 * template OWNS the rendering of all arrays — generalising the proven
 * `RepositoriesField` list body — so the broken defaults never appear.
 *
 * It is BODY-ONLY: `FieldTemplate` (RjsfTheme.tsx) already wraps every array
 * with its label + `FieldHelp` + error block, so this template renders just the
 * list rows + their controls, never a field label/description.
 *
 * Library-first: built from the shared `<Button>` primitive + lucide icons; no
 * drag library — up/down buttons keep reorder accessible and dependency-free.
 * Every color comes from an `hsl(var(--token))` CSS variable.
 */
import type { ArrayFieldTemplateProps } from "@rjsf/utils";
import { getUiOptions } from "@rjsf/utils";
import { ArrowDown, ArrowUp, Plus, Trash2 } from "lucide-react";
import { Button } from "../../ui/button";
import { helpCls } from "../styles";

/**
 * Optional per-item validator: `(value: string) => string | null`. When it
 * returns a non-null string the message renders as an inline `role="alert"`
 * below the row. Wired via `ui:options.itemValidator`.
 */
type ItemValidator = (value: string) => string | null;

/** formContext shape the Settings shell threads down (see RjsfTheme.tsx). */
interface SettingsFormContext {
  advanced?: boolean;
  originalData?: Record<string, unknown>;
  onRestore?: (id: string, value: unknown) => void;
}

export function ArrayFieldTemplate(props: ArrayFieldTemplateProps) {
  const {
    items,
    canAdd,
    onAddClick,
    disabled,
    readonly,
    idSchema,
    schema,
    uiSchema,
    formData,
    formContext,
  } = props;

  const editable = !disabled && !readonly;

  // ui:options — `itemValidator` (per-row validation) + `restoreDefault` (show
  // the "Restore defaults" button when a schema default exists).
  const options = getUiOptions(uiSchema) as {
    itemValidator?: unknown;
    restoreDefault?: unknown;
  };
  const itemValidator =
    typeof options.itemValidator === "function"
      ? (options.itemValidator as ItemValidator)
      : undefined;

  const ctx = (formContext ?? {}) as SettingsFormContext;
  const rows = Array.isArray(formData) ? (formData as unknown[]) : [];

  // Restore-defaults is opt-in AND requires both a schema default array and an
  // `onRestore` callback in formContext (the shell wires it later). Absent
  // either, the button simply doesn't render.
  const showRestore =
    Boolean(options.restoreDefault) &&
    Array.isArray((schema as { default?: unknown }).default) &&
    typeof ctx.onRestore === "function";

  return (
    <div className="space-y-2">
      {items.length === 0 ? (
        <p className={helpCls}>No entries.</p>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => {
            const error = itemValidator
              ? itemValidator(String(rows[item.index] ?? ""))
              : null;
            const rowId = `${item.key}`;
            const errorId = `${idSchema.$id}_${item.index}_error`;
            const rowEditable = editable && !item.disabled && !item.readonly;
            return (
              <li key={rowId} className="space-y-1">
                <div className="flex items-start gap-2">
                  <div className="flex-1 min-w-0">{item.children}</div>
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    iconOnly
                    disabled={!rowEditable || !item.hasMoveUp}
                    onClick={item.onReorderClick(item.index, item.index - 1)}
                    aria-label="Move entry up"
                    title="Move entry up"
                    leadingIcon={<ArrowUp className="w-4 h-4" />}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    iconOnly
                    disabled={!rowEditable || !item.hasMoveDown}
                    onClick={item.onReorderClick(item.index, item.index + 1)}
                    aria-label="Move entry down"
                    title="Move entry down"
                    leadingIcon={<ArrowDown className="w-4 h-4" />}
                  />
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    tone="danger"
                    iconOnly
                    disabled={!rowEditable || !item.hasRemove}
                    onClick={item.onDropIndexClick(item.index)}
                    aria-label="Remove entry"
                    title="Remove entry"
                    leadingIcon={<Trash2 className="w-4 h-4" />}
                  />
                </div>
                {error && (
                  <p
                    id={errorId}
                    role="alert"
                    className="text-[hsl(var(--destructive))] text-xs"
                  >
                    {error}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex items-center gap-2">
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={!editable || !canAdd}
          onClick={onAddClick}
          leadingIcon={<Plus className="w-4 h-4" />}
        >
          Add
        </Button>
        {showRestore && (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={!editable}
            onClick={() =>
              ctx.onRestore?.(
                idSchema.$id,
                (schema as { default?: unknown }).default
              )
            }
          >
            Restore defaults
          </Button>
        )}
      </div>
    </div>
  );
}
