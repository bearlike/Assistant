/**
 * SecretField — an RJSF widget for write-only secret config fields.
 *
 * The backend marks fields like `llm.api_key` / `langfuse.secret_key` as
 * `x-secret` → write-only: the VALUE is stripped from `GET /api/config` and
 * the schema, and "is it configured?" comes solely from the `secrets` is-set
 * map. So the widget never has a value to display — it has three states driven
 * by `options.secretConfigured` (injected by SettingsSection) plus a local
 * editing toggle:
 *
 *   - unconfigured  → empty password input; typing patches the value.
 *   - configured    → masked indicator + "Replace" → enters editing.
 *   - editing       → empty password input + "Cancel" → reverts (onChange
 *                     undefined) so an untouched secret never enters the patch.
 *
 * Never autosaves — the section footer's Save persists. The wrapping
 * FieldTemplate already renders the label/description, so this widget does not
 * duplicate them.
 *
 * Post-save reset: after a successful Save the section re-seeds its formData
 * from the PATCH response, but the backend never returns secret values, so the
 * widget gets `value=undefined` again WITHOUT remounting. SettingsSection
 * threads its `savedAt` counter through `ui:options`; we reset `editing` when
 * it changes so a saved secret snaps back to the masked "Configured" state
 * instead of dangling in an empty editing input.
 */
import { useEffect, useState } from "react";
import type { WidgetProps } from "@rjsf/utils";
import { Button } from "../ui/button";
import { inputBase } from "./styles";

export function SecretField(props: WidgetProps) {
  const { id, value, onChange, disabled, label, options } = props;
  const secretConfigured = options?.secretConfigured === true;
  const savedAt = typeof options?.savedAt === "number" ? options.savedAt : 0;
  const [editing, setEditing] = useState(false);

  // Leave editing mode whenever the section reports a save (savedAt bumps).
  useEffect(() => {
    setEditing(false);
  }, [savedAt]);

  // A password input — shared by the unconfigured and editing states. Empty by
  // design: the existing secret is never sent to the client.
  const passwordInput = (placeholder: string) => (
    <input
      id={id}
      type="password"
      className={inputBase}
      value={typeof value === "string" ? value : ""}
      onChange={(e) => onChange(e.target.value === "" ? undefined : e.target.value)}
      disabled={disabled}
      placeholder={placeholder}
      autoComplete="new-password"
      autoFocus={editing}
    />
  );

  // Configured + not editing → masked indicator with a Replace action.
  if (secretConfigured && !editing) {
    return (
      <div className="flex items-center gap-2">
        <span
          className="flex h-9 flex-1 items-center gap-2 rounded-md border border-[hsl(var(--border-strong))] bg-[hsl(var(--input))] px-3 text-sm text-[hsl(var(--muted-foreground))]"
          aria-label={label ? `${label} is configured` : "Configured"}
        >
          <span aria-hidden="true" className="tracking-widest">
            ••••••••
          </span>
          <span className="text-xs">Configured</span>
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => setEditing(true)}
        >
          Replace
        </Button>
      </div>
    );
  }

  // Editing an already-configured secret → input + Cancel (reverts the value).
  if (secretConfigured && editing) {
    return (
      <div className="flex items-center gap-2">
        <div className="flex-1">{passwordInput("Enter new value")}</div>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => {
            onChange(undefined);
            setEditing(false);
          }}
        >
          Cancel
        </Button>
      </div>
    );
  }

  // Unconfigured → plain password input.
  return passwordInput("Enter value");
}
