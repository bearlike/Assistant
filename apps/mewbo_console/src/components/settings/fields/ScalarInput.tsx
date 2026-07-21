/**
 * ScalarInput — the one control renderer for a single JSON-Schema scalar leaf.
 *
 * Replaces three near-identical `<input className={inputBase} .../>` (and, for
 * booleans, `<Switch>`) blocks that had drifted apart: KeyedCollectionField's
 * Subform property renderer, RecordListField's per-property inputs, and RJSF's
 * `BaseInputTemplate`. Dispatches purely on `type` — `"boolean"` renders a
 * `<Switch>`, anything else an `<input>` typed by `htmlType` (falling back to
 * `type`).
 *
 * Deliberately does NO value coercion of its own: `onChange` hands back the
 * raw DOM string (or the Switch's boolean) and the caller decides what
 * "cleared" means for that field. That decision genuinely differs per field —
 * compare RecordListField's `matcher` (empty means "unset", commits `null`)
 * against its `command` (empty is a valid value, no special-casing) — so
 * baking one policy in here would be wrong for at least one caller. It is
 * also why RJSF's `BaseInputTemplate` can keep its own default-coercion
 * `onChange` body byte-identical: only the markup moved here.
 *
 * NOT the same concern as wiki's `SettingsSelectField` (`ProjectSettingsDialog`):
 * that one wraps a `<select>` in react-hook-form's `FormItem`/`FormControl`/
 * `FormMessage` shell and spreads a `ControllerRenderProps` field. This is a bare
 * RJSF-adjacent control (raw `value`/`onChange`, no rhf, caller owns the chrome).
 * Different form stack and different control type — kept separate on purpose.
 */
import { Switch } from "../../ui/switch";
import { inputTextCls } from "../styles";

export interface ScalarInputProps {
  id: string;
  /** `"boolean"` renders a Switch; anything else renders a text-family input. */
  type?: string;
  /** Overrides the native `<input type>` when it must diverge from `type`
   *  (e.g. RJSF's widget `type="password"` on a schema `type: "string"`). */
  htmlType?: string;
  value: unknown;
  onChange: (value: string | boolean) => void;
  disabled?: boolean;
  readOnly?: boolean;
  required?: boolean;
  autoFocus?: boolean;
  autoComplete?: string;
  className?: string;
  onBlur?: (value: string) => void;
  onFocus?: (value: string) => void;
  "aria-label"?: string;
}

export function ScalarInput({
  id,
  type,
  htmlType,
  value,
  onChange,
  disabled,
  readOnly,
  required,
  autoFocus,
  autoComplete,
  className,
  onBlur,
  onFocus,
  ...aria
}: ScalarInputProps) {
  if (type === "boolean") {
    return (
      <Switch
        id={id}
        checked={Boolean(value)}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    );
  }

  return (
    <input
      id={id}
      type={htmlType ?? type ?? "text"}
      className={className ?? inputTextCls}
      value={value == null ? "" : String(value)}
      disabled={disabled}
      readOnly={readOnly}
      required={required}
      autoFocus={autoFocus}
      autoComplete={autoComplete}
      onChange={(e) => onChange(e.target.value)}
      onBlur={onBlur && ((e) => onBlur(e.target.value))}
      onFocus={onFocus && ((e) => onFocus(e.target.value))}
      {...aria}
    />
  );
}
