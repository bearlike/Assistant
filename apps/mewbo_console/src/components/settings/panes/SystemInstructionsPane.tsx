/**
 * SystemInstructionsPane — the custom system-instructions authoring surface,
 * hosted in the Agent facet (`../panes.ts`).
 *
 * A pane in the facet registry, so it takes ZERO props and fetches its own
 * data via `useSystemInstructions`/`useSystemInstructionsVariables` (TanStack
 * cache, shared by queryKey). Backed by `mewbo_core.system_instructions`: an
 * operator writes a Jinja template (Markdown prose + Jinja control flow) that
 * gets appended to the assistant's system prompt on every session. A template
 * that fails to COMPILE is rejected at save time (400, surfaced via the save
 * mutation's own `error`); a template that fails to RENDER inside a live session never
 * breaks the run, it just injects nothing and the failure lands on the
 * stored doc's `lastError`, surfaced here so a broken template is visible
 * even though nobody's session ever crashed because of it.
 *
 * Edit state is local to this pane (template/enabled), seeded once from the
 * fetched doc and reset on a successful save, mirroring `SettingsSection`'s
 * Save/Reset dirty-state contract (`SettingsSection.tsx:58-66`) without
 * routing through `SettingsModel` — this is a standalone document resource,
 * not a schema-sliced config section, so it owns its own diff instead of
 * borrowing the RJSF section machinery.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import CodeMirror, { type ReactCodeMirrorRef } from "@uiw/react-codemirror";
import { Loader2, RotateCcw, Save } from "lucide-react";
import {
  useSaveSystemInstructions,
  usePreviewSystemInstructions,
  useSystemInstructions,
  useSystemInstructionsVariables,
} from "../../../hooks/useSystemInstructions";
import { Button } from "../../ui/button";
import { Switch } from "../../ui/switch";
import { Label } from "../../ui/label";
import { ErrorAlert } from "../../ErrorAlert";
import { SettingsCard } from "../SettingsCard";
import { InstructionsPreview } from "./systemInstructions/InstructionsPreview";
import { VariableReference } from "./systemInstructions/VariableReference";
import { systemInstructionsExtensions } from "./systemInstructionsCodeMirror";

const SYSTEM_INSTRUCTIONS_HELP =
  "Custom system instructions are extra text you write once that gets added to the assistant's " +
  "system prompt on every session, on every surface, automatically.\n\n" +
  "Think of it as a standing note to the assistant that travels with it everywhere: house style, " +
  "things it should always remember about how you work, or rules that only apply on certain " +
  "surfaces. The template is Markdown prose with Jinja control flow mixed in, so you can write " +
  "plain instructions and also branch on context, for example writing one paragraph for a session " +
  "started on a phone and a different one for a session started at a terminal.\n\n" +
  "A broken template never breaks a session. If it fails to compile, saving is refused so you can " +
  "fix it before it reaches anyone. If it fails to render inside a live session, nothing is " +
  "injected for that turn and the failure is recorded below as the last render error, so a broken " +
  "template degrades quietly instead of derailing a run.\n\n" +
  "Turn it off with the toggle below to keep a template stored without having it take effect, and " +
  "use the preview panel to see exactly what a session on a given surface would receive before you save.";

export function SystemInstructionsPane() {
  const { doc, loading, error } = useSystemInstructions();
  const { variables, loading: variablesLoading, error: variablesError } =
    useSystemInstructionsVariables();
  const save = useSaveSystemInstructions();
  const preview = usePreviewSystemInstructions();

  const [template, setTemplate] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [seeded, setSeeded] = useState(false);
  const [savedAt, setSavedAt] = useState(0);
  const [previewSurface, setPreviewSurface] = useState("");
  const editorRef = useRef<ReactCodeMirrorRef>(null);

  // Seed local edit state exactly once, when the doc first arrives — a
  // background refetch (e.g. after save) must never clobber an in-flight edit.
  useEffect(() => {
    if (doc && !seeded) {
      setTemplate(doc.template);
      setEnabled(doc.enabled);
      setSeeded(true);
    }
  }, [doc, seeded]);

  // The surfaces a template can branch on are whatever the backend reports for
  // the `surface` variable, so the preview tabs ARE that list. The pane used to
  // hardcode six of them, which silently went stale the moment a new client
  // started stamping sessions.
  const surfaces = useMemo(
    () => variables.find((v) => v.name === "surface")?.values ?? [],
    [variables]
  );

  useEffect(() => {
    if (!previewSurface && surfaces.length > 0) setPreviewSurface(surfaces[0]);
  }, [surfaces, previewSurface]);

  const dirty = seeded && doc != null && (template !== doc.template || enabled !== doc.enabled);

  // Rebuilt when the variables land, NOT captured empty on the first render.
  // `variables` is referentially stable across renders (`useSystemInstructions`
  // hands back one shared empty array while the query is in flight), so this
  // memo recomputes exactly once: when the payload arrives.
  const editorExtensions = useMemo(() => systemInstructionsExtensions(variables), [variables]);

  /**
   * Drop a value at the cursor. Deliberately dumb: it inserts the bare string
   * and makes no attempt to guess whether it needs quoting, because the
   * operator is almost always already typing inside a quoted comparison, and a
   * wrong guess is worse than no guess.
   */
  const insertAtCursor = useCallback((value: string) => {
    const view = editorRef.current?.view;
    if (!view) return;
    const { from, to } = view.state.selection.main;
    view.dispatch({
      changes: { from, to, insert: value },
      selection: { anchor: from + value.length },
      scrollIntoView: true,
    });
    view.focus();
  }, []);

  const handleSave = async () => {
    try {
      await save.mutateAsync({ template, enabled });
      setSavedAt(Date.now());
    } catch {
      // Surfaced below via `save.error` — TanStack clears it on the next attempt.
    }
  };

  const handleReset = () => {
    if (!doc) return;
    setTemplate(doc.template);
    setEnabled(doc.enabled);
    save.reset();
  };

  const handlePreview = () => {
    preview.mutate({ template, context: { surface: previewSurface } });
  };

  return (
    <SettingsCard
      id="settings-system-instructions"
      title="Custom system instructions"
      description={SYSTEM_INSTRUCTIONS_HELP}
      footer={
        <>
          <Button
            type="button"
            variant="primary"
            size="md"
            disabled={!dirty || save.isPending}
            onClick={handleSave}
            leadingIcon={
              save.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Save className="w-4 h-4" />
              )
            }
          >
            {save.isPending ? "Saving…" : "Save"}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="md"
            disabled={!dirty || save.isPending}
            onClick={handleReset}
            leadingIcon={<RotateCcw className="w-4 h-4" />}
          >
            Reset
          </Button>
          {savedAt > 0 && !dirty && !save.isPending && (
            <span key={savedAt} className="text-xs text-[hsl(var(--success))]">
              Saved
            </span>
          )}
        </>
      }
    >
      {loading ? (
        <div className="py-10 text-center text-sm text-[hsl(var(--muted-foreground))]">
          Loading…
        </div>
      ) : error ? (
        <ErrorAlert
          title="Couldn't load system instructions"
          error={error}
          fallback="Failed to load system instructions"
        />
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-3">
            <Switch
              id="system-instructions-enabled"
              checked={enabled}
              onCheckedChange={setEnabled}
            />
            <Label htmlFor="system-instructions-enabled">
              Append to every session's system prompt
            </Label>
          </div>

          {doc?.lastError && (
            <ErrorAlert title="Last render error" error={doc.lastError} fallback={doc.lastError} />
          )}
          {save.error && (
            <ErrorAlert
              title="Couldn't save"
              error={save.error}
              fallback="Failed to save system instructions"
            />
          )}

          <div className="rounded-md overflow-hidden border border-[hsl(var(--code-border))]">
            <CodeMirror
              ref={editorRef}
              value={template}
              height="360px"
              theme="none"
              extensions={editorExtensions}
              onChange={setTemplate}
              basicSetup={{
                lineNumbers: true,
                foldGutter: true,
                highlightActiveLine: true,
                // Owned by `systemInstructionsExtensions`, which supplies the
                // variable-aware source. basicSetup's copy has no sources.
                autocompletion: false,
              }}
            />
          </div>

          <VariableReference
            variables={variables}
            loading={variablesLoading}
            error={variablesError}
            onInsert={insertAtCursor}
          />

          <InstructionsPreview
            surfaces={surfaces}
            previewSurface={previewSurface}
            onSurfaceChange={setPreviewSurface}
            onPreview={handlePreview}
            pending={preview.isPending}
            result={preview.data}
          />
        </div>
      )}
    </SettingsCard>
  );
}
