import { useCallback, useState } from "react";
import Form from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import type { IChangeEvent } from "@rjsf/core";
import type { RJSFSchema } from "@rjsf/utils";
import { useConfig } from "../hooks/useConfig";
import { rjsfTemplates, rjsfWidgets } from "./settings/RjsfTheme";
import { AlertTriangle, Loader2, Save, CheckCircle2 } from "lucide-react";
import { Button } from "./ui/button";

export function SettingsView() {
  const { schema, config, loading, saving, error, save } = useConfig();
  const [saveSuccess, setSaveSuccess] = useState(false);

  const handleSubmit = useCallback(
    async (e: IChangeEvent) => {
      if (!e.formData) return;
      setSaveSuccess(false);
      const ok = await save(e.formData as Record<string, unknown>);
      if (ok) {
        setSaveSuccess(true);
        setTimeout(() => setSaveSuccess(false), 3000);
      }
    },
    [save]
  );

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
      </div>
    );
  }

  if (!schema || !config) {
    return (
      <div className="flex-1 flex items-center justify-center">
        <p className="text-sm text-[hsl(var(--destructive))]">
          Failed to load configuration.
        </p>
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="max-w-2xl mx-auto px-6 py-8">
        <div className="mb-6">
          <h1 className="text-lg font-semibold text-[hsl(var(--foreground))]">
            Settings
          </h1>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-1">
            Some changes take effect on the next session.
          </p>
        </div>

        {error && (
          <div className="mb-4 flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
            <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive))] shrink-0 mt-0.5" />
            <p className="text-xs text-[hsl(var(--destructive))]">{error}</p>
          </div>
        )}

        {saveSuccess && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-[hsl(var(--success))]/30 bg-[hsl(var(--success))]/10 px-3 py-2.5">
            <CheckCircle2 className="w-4 h-4 text-[hsl(var(--success))] shrink-0" />
            <p className="text-xs text-[hsl(var(--success))]">Settings saved.</p>
          </div>
        )}

        <Form
          schema={schema as RJSFSchema}
          formData={config}
          validator={validator}
          templates={rjsfTemplates}
          widgets={rjsfWidgets}
          onSubmit={handleSubmit}
          liveValidate={false}
          noHtml5Validate
        >
          <Button
            type="submit"
            variant="primary"
            size="md"
            disabled={saving}
            leadingIcon={
              saving ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Save className="w-4 h-4" />
              )
            }
            className="mt-4"
          >
            {saving ? "Saving..." : "Save Settings"}
          </Button>
        </Form>
      </div>
    </div>
  );
}
