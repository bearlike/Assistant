/* eslint-disable react-refresh/only-export-components */
/**
 * Tailwind-styled RJSF templates for the Truss console.
 *
 * Renders plain HTML styled with the project's HSL CSS variables.
 * No UI component library is introduced.
 */
import {
  FieldTemplateProps,
  ObjectFieldTemplateProps,
  BaseInputTemplateProps,
  WidgetProps,
  RegistryWidgetsType,
  TemplatesType,
} from "@rjsf/utils";

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
      <summary className="cursor-pointer select-none px-4 py-3 text-sm font-semibold text-[hsl(var(--foreground))] hover:bg-[hsl(var(--accent))] transition-colors">
        {title || idSchema.$id}
        {description && (
          <span className="ml-2 text-xs font-normal text-[hsl(var(--muted-foreground))]">
            {description}
          </span>
        )}
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
    description,
    schema,
    displayLabel,
  } = props;

  // Don't wrap objects — ObjectFieldTemplate handles them
  if (schema.type === "object") {
    return children;
  }

  return (
    <div className="space-y-1">
      {displayLabel && label && (
        <label
          htmlFor={id}
          className="block text-xs font-medium text-[hsl(var(--foreground))]"
        >
          {label}
        </label>
      )}
      {children}
      {displayLabel && description && (
        <p className="text-xs text-[hsl(var(--muted-foreground))]">
          {description}
        </p>
      )}
      {errors}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Input widgets
// ---------------------------------------------------------------------------

const inputBase =
  "w-full rounded-md border border-[hsl(var(--border-strong))] bg-[hsl(var(--input))] " +
  "px-3 py-1.5 text-sm text-[hsl(var(--foreground))] placeholder:text-[hsl(var(--muted-foreground))] " +
  "focus:outline-none focus:ring-1 focus:ring-[hsl(var(--ring))]";

function BaseInputTemplate(props: BaseInputTemplateProps) {
  const { id, type, value, onChange, onBlur, onFocus, readonly, disabled, autofocus } = props;
  return (
    <input
      id={id}
      type={type || "text"}
      className={inputBase}
      value={value ?? ""}
      onChange={(e) => onChange(e.target.value === "" ? props.schema.default : e.target.value)}
      onBlur={onBlur && ((e) => onBlur(id, e.target.value))}
      onFocus={onFocus && ((e) => onFocus(id, e.target.value))}
      readOnly={readonly}
      disabled={disabled}
      autoFocus={autofocus}
    />
  );
}

function CheckboxWidget(props: WidgetProps) {
  const { id, value, onChange, readonly, disabled, label } = props;
  return (
    <label htmlFor={id} className="flex items-center gap-2 cursor-pointer">
      <input
        id={id}
        type="checkbox"
        checked={!!value}
        onChange={(e) => onChange(e.target.checked)}
        readOnly={readonly}
        disabled={disabled}
        className="h-4 w-4 rounded border-[hsl(var(--border-strong))] bg-[hsl(var(--input))] text-[hsl(var(--primary))] focus:ring-[hsl(var(--ring))]"
      />
      <span className="text-xs text-[hsl(var(--foreground))]">{label}</span>
    </label>
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
};

export const rjsfTemplates: Partial<TemplatesType> = {
  ObjectFieldTemplate,
  FieldTemplate,
  BaseInputTemplate,
};
