/**
 * SettingsSection — one controlled section card in the faceted Settings shell.
 *
 * The shell owns edit state (a sectionId → formData map); this component is a
 * thin, controlled renderer over a single section's sliced RJSF schema. All
 * grouping / slicing / diff logic lives in `SettingsModel` — this file only
 * composes the form, the Save/Reset footer, and a transient "Saved"
 * announcement. No settings business logic is duplicated here.
 */
import { useMemo, useState } from "react";
import Form from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import type { IChangeEvent } from "@rjsf/core";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { Loader2, RotateCcw, Save } from "lucide-react";
import type { SettingsModel } from "./SettingsModel";
import { rjsfFields, rjsfTemplates, rjsfWidgets } from "./RjsfTheme";
import { Button } from "../ui/button";

export interface SettingsSectionProps {
  model: SettingsModel;
  sectionId: string;
  value: Record<string, unknown>;
  original: Record<string, unknown>;
  advanced: boolean;
  /** Full dot-path → is-set map from `GET /api/config` (drives secret widgets). */
  secrets: Record<string, boolean>;
  onChange: (next: Record<string, unknown>) => void;
  onSave: () => Promise<void>;
}

export function SettingsSection({
  model,
  sectionId,
  value,
  original,
  advanced,
  secrets,
  onChange,
  onSave,
}: SettingsSectionProps) {
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState(0);

  const section = model.section(sectionId);
  const dirty = model.isDirty(sectionId, value);
  const headingId = `settings-section-${sectionId}`;

  // Widget/field routing is owned by the model so the schema → ui:schema
  // mapping lives in one tested place (secret widgets, record-list /
  // keyed-collection fields, per-array ui:options).
  const uiSchema = useMemo<UiSchema>(
    () => model.uiSchemaFor(sectionId, secrets, savedAt) as UiSchema,
    [model, sectionId, secrets, savedAt]
  );

  const handleSave = async () => {
    setSaving(true);
    try {
      await onSave();
      setSavedAt(Date.now());
    } finally {
      setSaving(false);
    }
  };

  return (
    <section
      aria-labelledby={headingId}
      className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5"
    >
      <div className="mb-4">
        <h2
          id={headingId}
          className="text-sm font-semibold text-[hsl(var(--foreground))]"
        >
          {section?.title ?? sectionId}
        </h2>
        {section?.description && (
          <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
            {section.description}
          </p>
        )}
      </div>

      <Form
        schema={model.sliceSchema(sectionId) as RJSFSchema}
        uiSchema={uiSchema}
        formData={value}
        formContext={{
          originalData: original,
          advanced,
          // Restore-defaults seam for ArrayFieldTemplate (e.g. the top-level
          // `plan_mode_shell_allowlist`): write the default array back into the
          // section by stripping RJSF's `root_` id prefix to recover the key.
          onRestore: (fid: string, val: unknown) =>
            onChange({ ...value, [fid.replace(/^root_/, "")]: val }),
        }}
        templates={rjsfTemplates}
        widgets={rjsfWidgets}
        fields={rjsfFields}
        validator={validator}
        onChange={(e: IChangeEvent) =>
          onChange((e.formData ?? {}) as Record<string, unknown>)
        }
        liveValidate={false}
        noHtml5Validate
      >
        {/* Suppress RJSF's built-in submit button — the footer owns Save. */}
        <></>
      </Form>

      <div className="mt-4 flex items-center gap-2 border-t border-[hsl(var(--border))] pt-4">
        <Button
          type="button"
          variant="primary"
          size="md"
          disabled={!dirty || saving}
          onClick={handleSave}
          leadingIcon={
            saving ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Save className="w-4 h-4" />
            )
          }
        >
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button
          type="button"
          variant="ghost"
          size="md"
          disabled={!dirty || saving}
          onClick={() => onChange(original)}
          leadingIcon={<RotateCcw className="w-4 h-4" />}
        >
          Reset
        </Button>

        <span aria-live="polite" className="sr-only">
          {savedAt ? "Saved" : ""}
        </span>
        {savedAt > 0 && !dirty && !saving && (
          <span
            key={savedAt}
            className="text-xs text-[hsl(var(--success))]"
          >
            Saved
          </span>
        )}
      </div>
    </section>
  );
}
