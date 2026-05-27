/* eslint-disable react-refresh/only-export-components */
/**
 * Tailwind-styled RJSF templates for the Mewbo console.
 *
 * Renders plain HTML styled with the project's HSL CSS variables, plus the
 * shared shadcn `<Switch>` for booleans. The faceted Settings shell drives two
 * behaviours through RJSF `formContext`:
 *
 *   - `advanced` — when false, fields flagged `x-advanced` are hidden.
 *   - `originalData` — the section's pristine config; leaf fields whose value
 *     differs from it get a brand-tinted left border (the "changed row" cue).
 */
import {
  FieldTemplateProps,
  ObjectFieldTemplateProps,
  BaseInputTemplateProps,
  WidgetProps,
  RegistryWidgetsType,
  RegistryFieldsType,
  TemplatesType,
} from "@rjsf/utils";
import { Switch } from "../ui/switch";
import { SecretField } from "./SecretField";
import { ArrayFieldTemplate } from "./fields/ArrayField";
import { RecordListField } from "./fields/RecordListField";
import { KeyedCollectionField } from "./fields/KeyedCollectionField";
import { FieldLabel } from "./fields/FieldLabel";
import { FieldHelp } from "./fields/FieldHelp";
import { inputBase, subsectionTitleCls } from "./styles";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// formContext — supplied by the Settings shell / SettingsSection
// ---------------------------------------------------------------------------

interface SettingsFormContext {
  /** Show fields flagged `x-advanced`. */
  advanced?: boolean;
  /** Pristine section config, used to flag modified leaves. */
  originalData?: Record<string, unknown>;
}

const isObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/**
 * Resolve the original value for a field given its RJSF `id`. RJSF ids look
 * like `root_lsp_enabled` and the default separator is `_`, which is ambiguous
 * because keys can themselves contain `_`. We walk `originalData` greedily:
 * at each level, consume the longest leading run of separator-joined tokens
 * that matches an existing key. Returns a sentinel `undefined`-distinct miss
 * via the `found` flag so we never draw a false border on a genuine miss.
 */
function resolveOriginal(
  id: string,
  original: Record<string, unknown> | undefined
): { found: boolean; value: unknown } {
  if (!original || !id.startsWith("root")) return { found: false, value: undefined };
  const rest = id.slice("root".length).replace(/^_/, "");
  if (!rest) return { found: false, value: undefined };
  const tokens = rest.split("_");

  let node: unknown = original;
  let i = 0;
  while (i < tokens.length) {
    if (!isObject(node)) return { found: false, value: undefined };
    // Greedily match the longest key built from the remaining tokens.
    let matched = false;
    for (let take = tokens.length - i; take >= 1; take--) {
      const key = tokens.slice(i, i + take).join("_");
      if (Object.prototype.hasOwnProperty.call(node, key)) {
        node = (node as Record<string, unknown>)[key];
        i += take;
        matched = true;
        break;
      }
    }
    if (!matched) return { found: false, value: undefined };
  }
  return { found: true, value: node };
}

// ---------------------------------------------------------------------------
// Object / section template
// ---------------------------------------------------------------------------

function ObjectFieldTemplate(props: ObjectFieldTemplateProps) {
  const { title, description, properties, idSchema } = props;
  const isRoot = idSchema.$id === "root";

  if (isRoot) {
    return <div className="space-y-3">{properties.map((p) => p.content)}</div>;
  }

  return (
    <details className="group rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden">
      <summary className="cursor-pointer select-none px-4 py-3 hover:bg-[hsl(var(--accent))] transition-colors space-y-1">
        <span className={subsectionTitleCls}>{title || idSchema.$id}</span>
        <FieldHelp text={typeof description === "string" ? description : undefined} />
      </summary>
      <div className="px-4 pb-4 pt-2 space-y-3 border-t border-[hsl(var(--border))]">
        {properties.map((p) => p.content)}
      </div>
    </details>
  );
}

// ---------------------------------------------------------------------------
// Field template (label + description + error)
// ---------------------------------------------------------------------------

function FieldTemplate(props: FieldTemplateProps) {
  const {
    id,
    label,
    children,
    errors,
    schema,
    displayLabel,
    formData,
    formContext,
  } = props;
  const ctx = (formContext ?? {}) as SettingsFormContext;

  // Deprecated fields never render (defence-in-depth; the model also filters
  // them out of the slice, but a nested $ref could still surface one).
  if ((schema as Record<string, unknown>).deprecated === true) {
    return null;
  }

  // Don't wrap objects — ObjectFieldTemplate handles them.
  if (schema.type === "object") {
    return children;
  }

  // Advanced-hide: drop fields flagged `x-advanced` unless advanced is on.
  if ((schema as Record<string, unknown>)["x-advanced"] === true && !ctx.advanced) {
    return null;
  }

  // Modified-border: brand-tinted left rail when the leaf differs from its
  // pristine value. Robust — a resolution miss simply skips the cue.
  const { found, value: originalValue } = resolveOriginal(id, ctx.originalData);
  const modified =
    found && JSON.stringify(originalValue) !== JSON.stringify(formData);

  // Render help from the RAW schema string (not RJSF's DescriptionField
  // ReactNode) so FieldHelp can render markdown.
  const help =
    typeof schema.description === "string" ? schema.description : undefined;

  return (
    <div className={cn("space-y-1", modified && "border-l-2 border-[hsl(var(--primary))] pl-2")}>
      {displayLabel && label && <FieldLabel htmlFor={id}>{label}</FieldLabel>}
      {children}
      {displayLabel && <FieldHelp text={help} id={`${id}__help`} />}
      {errors}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Input widgets
// ---------------------------------------------------------------------------

function BaseInputTemplate(props: BaseInputTemplateProps) {
  const { id, type, value, onChange, onBlur, onFocus, readonly, disabled, autofocus } = props;
  const isPassword = type === "password";
  return (
    <input
      id={id}
      type={type || "text"}
      className={inputBase}
      value={value ?? ""}
      onChange={(e) => {
        const v = e.target.value;
        if (v === "") {
          onChange(props.schema.default ?? undefined);
          return;
        }
        // Coerce numeric schema fields to numbers so the stored value matches
        // the config (and JSON-diff): a raw string "5" would read as a
        // permanent diff against the numeric original 5.
        const t = props.schema.type;
        if (t === "number" || t === "integer") {
          const n = Number(v);
          onChange(Number.isFinite(n) ? n : v);
        } else {
          onChange(v);
        }
      }}
      onBlur={onBlur && ((e) => onBlur(id, e.target.value))}
      onFocus={onFocus && ((e) => onFocus(id, e.target.value))}
      readOnly={readonly}
      disabled={disabled}
      autoFocus={autofocus}
      autoComplete={isPassword ? "new-password" : undefined}
    />
  );
}

/**
 * Boolean widget — RJSF maps `type: "boolean"` to `CheckboxWidget`, so we
 * register the shadcn `<Switch>` under that name. Label + description stay
 * inline so the toggle reads as a labelled control.
 */
function CheckboxWidget(props: WidgetProps) {
  const { id, value, onChange, readonly, disabled, label, schema } = props;
  const description = typeof schema.description === "string" ? schema.description : undefined;
  return (
    <div className="flex items-start gap-3">
      <Switch
        id={id}
        checked={!!value}
        onCheckedChange={(checked) => onChange(checked)}
        disabled={disabled || readonly}
        aria-label={label || undefined}
        className="mt-0.5"
      />
      {(label || description) && (
        <label htmlFor={id} className="cursor-pointer select-none">
          {label && (
            <span className="block text-xs font-medium text-[hsl(var(--foreground))]">
              {label}
            </span>
          )}
          {description && (
            <span className="block text-xs text-[hsl(var(--muted-foreground))]">
              {description}
            </span>
          )}
        </label>
      )}
    </div>
  );
}

function SelectWidget(props: WidgetProps) {
  const { id, value, options, onChange, readonly, disabled } = props;
  const enumOptions = (options as { enumOptions?: { value: string; label: string }[] }).enumOptions ?? [];
  return (
    <select
      id={id}
      className={inputBase}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value)}
      disabled={disabled || readonly}
    >
      {enumOptions.map((opt) => (
        <option key={opt.value} value={opt.value}>
          {opt.label}
        </option>
      ))}
    </select>
  );
}

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

export const rjsfWidgets: RegistryWidgetsType = {
  CheckboxWidget,
  SelectWidget,
  // Write-only secret fields — wired by SettingsSection via
  // `ui:widget: "secret"` + `ui:options.secretConfigured`.
  secret: SecretField,
};

export const rjsfFields: RegistryFieldsType = {
  // list[BaseModel] editor (the HooksConfig arrays) — wired via
  // `ui:field: "recordList"`.
  recordList: RecordListField,
  // dict[str, X] map editor (projects, channels, model_context_windows,
  // lsp.servers) — wired via `ui:field: "keyedCollection"`.
  keyedCollection: KeyedCollectionField,
};

export const rjsfTemplates: Partial<TemplatesType> = {
  ObjectFieldTemplate,
  FieldTemplate,
  BaseInputTemplate,
  ArrayFieldTemplate,
};
