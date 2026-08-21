/**
 * ProjectSettingsDialog — edit an already-onboarded wiki project's indexing
 * settings. Reachable from the WikiTopBar gear on every in-project
 * screen and from the gallery card.
 *
 * Non-obvious rules this file holds:
 *
 * - **Settings apply on the NEXT index.** A PATCH re-runs nothing; the dialog
 *   says so next to Save, because "changed the model, wiki still reads the same"
 *   is otherwise indistinguishable from a bug.
 * - **The server's `editable` map is the sole gate.** A field is offered only
 *   when the server flags it `true` (`canEdit`) — fail-closed. That's what keeps
 *   `graphOnly`'s developer-mode gate honest on the client, and it's what
 *   collapses the reduced catalog shape to its display fields with no branching.
 * - **Only the dirty subset is PATCHed** (`buildPatch` over rhf's `dirtyFields`),
 *   so an untouched field is never echoed back at the server as a "change".
 * - **Identity is not editable.** Repo URL + slug render read-only: the slug keys
 *   pages/jobs/credentials, so re-pointing it is a delete + recreate, not a PATCH.
 * - **No inline token field, ever.** Credential coverage is a read-only line
 *   sourced from the server's resolved `credential` (the ONE credential chain), with a
 *   deep link to Settings → Repositories to change it.
 * - **Errors land inline, never as a toast**: a 403 (developer-mode gate) pins to
 *   the graph-only switch, field-level 400s pin to their fields, anything else
 *   (e.g. a 409) fills the dialog banner. Success = close + list refresh.
 */

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useForm, type ControllerRenderProps } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import {
  AlertTriangle,
  ChevronDown,
  ExternalLink,
  GitBranch,
  Globe,
  Loader2,
  Network,
  RotateCcw,
  Save,
  ShieldCheck,
  ShieldOff,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Form,
  FormControl,
  FormDescription,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { hostOf, matchCredential } from "@/api/git";
import { ModelFallbackChain } from "@/components/ModelFallbackChain";
import { useGitCredentials } from "@/hooks/useGitCredentials";
import { useModels } from "@/hooks/useModels";
import { cn } from "@/lib/utils";

import { ModelPicker } from "./ModelPicker";
import { isWikiError } from "./api/client";
import {
  useBranches,
  useProjectSettings,
  useUpdateProject,
  useWikiDefaults,
  useWikiLanguages,
  useWikiProjectBySlug,
} from "./api/hooks";
import {
  isCatalogSettings,
  type ProjectSettingsCredential,
  type ProjectSettingsField,
} from "./api/types";
import {
  attachedServerNames,
  buildPatch,
  EMPTY_FORM,
  formFieldFor,
  needsReindex,
  seedFrom,
  settingsSchema,
  splitLines,
  type SettingsValues,
} from "./projectSettingsForm";

// ── Small presentational pieces ───────────────────────────────────────────

const selectCls =
  "flex-1 bg-transparent text-sm outline-none appearance-none pr-6 cursor-pointer text-[hsl(var(--foreground))] disabled:cursor-default";
const selectShellCls =
  "relative flex items-center gap-2 h-10 px-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))] focus-within:border-[hsl(var(--border-strong))] focus-within:ring-2 focus-within:ring-[hsl(var(--primary))]";
// `text-field md:text-sm`, not a bare `text-xs`: this feeds a real `<Textarea>`
// (the dirs/files pattern lists below), and `Textarea`'s `cn()` merge is
// last-wins, so a bare size here would replace the primitive's 16px iOS zoom
// floor instead of composing with it.
const textareaCls =
  "w-full font-mono text-field md:text-sm bg-[hsl(var(--muted))] border-[hsl(var(--border))] rounded-lg p-2.5 resize-none";

/** The four string-enum select fields this dialog renders (Branch / Depth /
 *  Language / Filter mode) — S2: they were four independently hand-copied
 *  `FormField` blocks around the same `selectShellCls`/`selectCls`/`ChevronDown`
 *  shell; this is the one wrapper they all render through now.
 *
 *  This is the react-hook-form `<select>` wrapper (spreads a Controller `field`,
 *  owns the `FormItem`/`FormMessage` chrome). It is deliberately NOT unified with
 *  settings/fields/`ScalarInput` (the RJSF-adjacent bare input/switch leaf) —
 *  different form stack, different control type. */
type SelectFieldName = "branch" | "depth" | "language" | "filterMode";

function SettingsSelectField({
  label,
  icon,
  field,
  disabled,
  children,
}: {
  label: string;
  icon?: ReactNode;
  field: ControllerRenderProps<SettingsValues, SelectFieldName>;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <FormItem className="space-y-1.5">
      <FormLabel>{label}</FormLabel>
      <FormControl>
        <div className={selectShellCls}>
          {icon}
          <select {...field} aria-label={label} disabled={disabled} className={selectCls}>
            {children}
          </select>
          <ChevronDown className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] pointer-events-none absolute right-3" />
        </div>
      </FormControl>
      <FormMessage />
    </FormItem>
  );
}

function DialogBanner({ message }: { message: string }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
      <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive-text))] shrink-0 mt-0.5" />
      <p className="text-xs text-[hsl(var(--destructive-text))]">{message}</p>
    </div>
  );
}

/** Read-only credential coverage + the ONE place to change it. */
function CredentialLine({ credential }: { credential: ProjectSettingsCredential }) {
  const shared = credential.scopeType === "host";
  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 px-3 py-2.5 space-y-1.5">
      <div className="flex items-start gap-2">
        {credential.present ? (
          <ShieldCheck className="h-3.5 w-3.5 text-[hsl(var(--primary))] shrink-0 mt-0.5" />
        ) : (
          <ShieldOff className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] shrink-0 mt-0.5" />
        )}
        <p className="text-xs text-[hsl(var(--muted-foreground))] flex-1 min-w-0">
          {credential.present ? (
            <>
              Cloned with the saved credential for{" "}
              <code className="font-mono text-[hsl(var(--foreground))]">
                {credential.scope}
              </code>
              {shared ? " (shared across this host)" : " (pinned to this repo)"}.
            </>
          ) : (
            <>
              No stored credential covers this repo — indexing falls back to the
              host&apos;s ambient git credential.
            </>
          )}
        </p>
      </div>
      <a
        href="/settings?facet=repositories"
        className="inline-flex items-center gap-1 text-2xs text-[hsl(var(--primary-text))] hover:underline"
      >
        Manage in Settings → Repositories
        <ExternalLink className="size-3" />
      </a>
    </div>
  );
}

// ── Dialog ────────────────────────────────────────────────────────────────

interface ProjectSettingsDialogProps {
  slug: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The screen's EXISTING re-index CTA opener. A PATCH re-runs nothing, so
   *  after saving an index-time field we hand off to that one CTA rather than
   *  minting a second refresh path (same rule the FreshnessBadge follows).
   *  Absent → the dialog just closes and the copy carries the message. */
  onRefresh?: () => void;
}

export function ProjectSettingsDialog({
  slug,
  open,
  onOpenChange,
  onRefresh,
}: ProjectSettingsDialogProps) {
  const settingsQuery = useProjectSettings(slug, open);
  const settings = settingsQuery.data;

  // Repo URL is display-only identity — read it off the already-cached projects
  // list rather than adding it to the settings contract.
  const project = useWikiProjectBySlug(slug);
  const repoUrl = project.data?.repoUrl ?? "";

  const languages = useWikiLanguages();
  const branchesQuery = useBranches({ repoUrl });
  const { credentials } = useGitCredentials(open);

  const [banner, setBanner] = useState<string | null>(null);
  const updateM = useUpdateProject(slug);
  const { models: allModels } = useModels();
  const wikiDefaults = useWikiDefaults();
  // UI-only: the wire has no "enabled" field, so an armed-but-empty ladder has
  // to be expressible locally or the switch would flip itself back off.
  const [fallbackOn, setFallbackOn] = useState(false);

  const form = useForm<SettingsValues>({
    resolver: zodResolver(settingsSchema),
    defaultValues: EMPTY_FORM,
  });

  // Seed once the settings land (and on every re-open, so a cancelled edit
  // doesn't linger). `reset` also re-baselines `dirtyFields`, which is what the
  // dirty-subset PATCH is computed from.
  useEffect(() => {
    if (open && settings) {
      form.reset(seedFrom(settings));
      setFallbackOn((settings.fallbackModels?.length ?? 0) > 0);
    }
    if (open) setBanner(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, settings]);

  /** The server's edit-safety map is the sole gate — fail closed. */
  const canEdit = (field: ProjectSettingsField): boolean =>
    settings?.editable?.[field] === true;
  // Names only — the entries never leave the server (they can carry
  // credentials), so this is all the dialog can show and all it needs to.
  const attachedNames = settings ? attachedServerNames(settings) : [];

  const credential: ProjectSettingsCredential = useMemo(() => {
    if (settings?.credential) return settings.credential;
    // Fallback for a payload that predates the resolved field: guess from the
    // credential list the same way the onboarding wizard hints.
    const match = matchCredential(credentials, {
      slug,
      host: hostOf(repoUrl || slug),
    });
    return match
      ? { present: true, scope: match.scope, scopeType: match.scopeType }
      : { present: false, scope: null, scopeType: null };
  }, [settings, credentials, slug, repoUrl]);

  const refValue = form.watch("branch");
  const filterMode = form.watch("filterMode");
  // Keep the pinned ref selectable even when the branch list is still loading
  // (or the remote can't be reached) — otherwise opening the dialog would
  // silently reset the project's branch to "default" on save.
  const branchOptions = useMemo(() => {
    const list = branchesQuery.data?.branches ?? [];
    return refValue && !list.includes(refValue) ? [refValue, ...list] : list;
  }, [branchesQuery.data, refValue]);

  const onSubmit = form.handleSubmit((values) => {
    const patch = buildPatch(values, form.formState.dirtyFields);
    if (Object.keys(patch).length === 0) {
      onOpenChange(false);
      return;
    }
    setBanner(null);
    updateM.mutate(patch, {
      onSuccess: () => {
        onOpenChange(false);
        // Saving changed nothing on disk yet — an index-time edit only lands on
        // the next run. Hand straight off to the screen's own re-index CTA so
        // the user isn't left wondering why the wiki still reads the same.
        if (needsReindex(patch)) onRefresh?.();
      },
      onError: (err) => {
        const wikiErr = isWikiError(err) ? err : null;
        let pinned = false;
        // 400 validation → each field error lands on its own input.
        for (const [wire, message] of Object.entries(wikiErr?.fields ?? {})) {
          const name = formFieldFor(wire);
          if (name) {
            form.setError(name, { message });
            pinned = true;
          }
        }
        // 403 is the developer-mode gate on graph-only — pin it to that switch.
        if (wikiErr?.code === "forbidden" && canEdit("graphOnly")) {
          form.setError("graphOnly", { message: wikiErr.message });
          pinned = true;
        }
        // Anything else (409 identity edit, 5xx, transport) → dialog banner.
        // Never a toast: the error belongs where the edit was made.
        if (!pinned) {
          setBanner(
            wikiErr?.message ??
              (err instanceof Error ? err.message : "Failed to save settings"),
          );
        }
      },
    });
  });

  // `desc` writes through immediately; every other field waits for the next
  // index. Say which of those two the pending edit actually is.
  const dirtyNow = form.formState.dirtyFields;
  const descOnlyEdit =
    Boolean(dirtyNow.desc) &&
    !needsReindex(buildPatch(form.getValues(), dirtyNow));

  const isCatalog = settings != null && isCatalogSettings(settings);
  const nothingEditable =
    settings != null &&
    !Object.values(settings.editable ?? {}).some(Boolean);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg max-h-[85vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Wiki settings</DialogTitle>
          <DialogDescription>
            <code className="font-mono text-[hsl(var(--foreground))]">{slug}</code>
            {repoUrl && (
              <>
                {" · "}
                <span className="font-mono break-all">{repoUrl}</span>
              </>
            )}
            <span className="block mt-1">
              The repository is this wiki&apos;s identity — to point at a different
              repo, delete this wiki and create a new one.
            </span>
          </DialogDescription>
        </DialogHeader>

        {settingsQuery.isPending && (
          <div className="flex items-center justify-center py-10">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {settingsQuery.isError && (
          <DialogBanner
            message={
              settingsQuery.error instanceof Error
                ? settingsQuery.error.message
                : "Couldn't load this project's settings."
            }
          />
        )}

        {settings && (
          <Form {...form}>
            <form onSubmit={onSubmit} className="space-y-4">
              {isCatalog && (
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  This is a document catalog — it has no repository, so branch,
                  scope, and clone settings don&apos;t apply.
                </p>
              )}

              {nothingEditable && (
                <p className="text-xs text-[hsl(var(--muted-foreground))]">
                  This project has no editable settings.
                </p>
              )}

              {/* Model and its fallback ladder render as ONE tabbed picker
                  when both are server-editable — the same merged mechanism
                  `ModelSelector`/the wizard use. Nested `FormField`s stay
                  independent rhf `Controller`s (own field name, own dirty
                  tracking, own `FormMessage`), so the per-field PATCH gate
                  above ("Only the DIRTY subset is PATCHed") is unaffected;
                  this only changes how the two controls are LAID OUT. */}
              {canEdit("model") && canEdit("fallbackModels") && (
                <FormField
                  control={form.control}
                  name="model"
                  render={({ field: modelField }) => (
                    <FormField
                      control={form.control}
                      name="fallbackModels"
                      render={({ field: fallbackField }) => (
                        <FormItem className="space-y-1.5">
                          <FormLabel>Model</FormLabel>
                          <FormControl>
                            <ModelPicker
                              value={modelField.value}
                              onChange={modelField.onChange}
                              variant="full"
                              fallbackEnabled={fallbackOn}
                              fallbackModels={splitLines(fallbackField.value)}
                              onFallbackEnabledChange={(on) => {
                                setFallbackOn(on);
                                if (!on) fallbackField.onChange("");
                              }}
                              onFallbackModelsChange={(next) =>
                                fallbackField.onChange(next.join("\n"))
                              }
                            />
                          </FormControl>
                          <FormMessage />
                        </FormItem>
                      )}
                    />
                  )}
                />
              )}

              {canEdit("embeddingModel") && (
                <FormField
                  control={form.control}
                  name="embeddingModel"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>Embedding model</FormLabel>
                      <FormDescription>
                        Leave blank to inherit the deployment default. Changing this
                        model requires a full re-index before the new setting applies.
                      </FormDescription>
                      <FormControl>
                        <Input
                          {...field}
                          aria-label="Embedding model"
                          placeholder={wikiDefaults.data?.embeddingModel ?? "Deployment default"}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {canEdit("model") && !canEdit("fallbackModels") && (
                <FormField
                  control={form.control}
                  name="model"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>Model</FormLabel>
                      <FormControl>
                        <ModelPicker
                          value={field.value}
                          onChange={field.onChange}
                          variant="full"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {!canEdit("model") && canEdit("fallbackModels") && (
                <FormField
                  control={form.control}
                  name="fallbackModels"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>Fallback</FormLabel>
                      <FormControl>
                        <ModelFallbackChain
                          models={allModels}
                          value={splitLines(field.value)}
                          onChange={(next) => field.onChange(next.join("\n"))}
                          enabled={fallbackOn}
                          onEnabledChange={(on) => {
                            setFallbackOn(on);
                            if (!on) field.onChange("");
                          }}
                          className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {canEdit("ref") && (
                <FormField
                  control={form.control}
                  name="branch"
                  render={({ field }) => (
                    <SettingsSelectField
                      label="Branch"
                      icon={<GitBranch className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />}
                      field={field}
                      disabled={branchesQuery.isLoading}
                    >
                      <option value="">
                        {branchesQuery.data?.defaultBranch
                          ? `Default · ${branchesQuery.data.defaultBranch}`
                          : "Default branch"}
                      </option>
                      {branchOptions.map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </SettingsSelectField>
                  )}
                />
              )}

              <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
                {canEdit("depth") && (
                  <FormField
                    control={form.control}
                    name="depth"
                    render={({ field }) => (
                      <SettingsSelectField label="Depth" field={field}>
                        <option value="comprehensive">Comprehensive</option>
                        <option value="concise">Concise</option>
                      </SettingsSelectField>
                    )}
                  />
                )}

                {canEdit("language") && (
                  <FormField
                    control={form.control}
                    name="language"
                    render={({ field }) => (
                      <SettingsSelectField
                        label="Language"
                        icon={<Globe className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))]" />}
                        field={field}
                      >
                        {(languages.data ?? []).map((l) => (
                          <option key={l.id} value={l.id}>
                            {l.label}
                          </option>
                        ))}
                      </SettingsSelectField>
                    )}
                  />
                )}
              </div>

              {canEdit("filterMode") && (
                <FormField
                  control={form.control}
                  name="filterMode"
                  render={({ field }) => (
                    <SettingsSelectField label="Filter mode" field={field}>
                      <option value="exclude">
                        Exclude paths — index everything else
                      </option>
                      <option value="include">
                        Include only — index just these paths
                      </option>
                    </SettingsSelectField>
                  )}
                />
              )}

              <div className="grid gap-3 grid-cols-1 sm:grid-cols-2">
                {canEdit("dirs") && (
                  <FormField
                    control={form.control}
                    name="dirs"
                    render={({ field }) => (
                      <FormItem className="space-y-1.5">
                        <FormLabel>
                          {filterMode === "include"
                            ? "Directories to include"
                            : "Directories to exclude"}
                        </FormLabel>
                        <FormControl>
                          <Textarea
                            {...field}
                            rows={5}
                            spellCheck={false}
                            placeholder={"node_modules\ndist"}
                            className={textareaCls}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                )}

                {canEdit("files") && (
                  <FormField
                    control={form.control}
                    name="files"
                    render={({ field }) => (
                      <FormItem className="space-y-1.5">
                        <FormLabel>
                          {filterMode === "include"
                            ? "Files to include"
                            : "Files to exclude"}
                        </FormLabel>
                        <FormControl>
                          <Textarea
                            {...field}
                            rows={5}
                            spellCheck={false}
                            placeholder={"*.lock\n*.min.js"}
                            className={textareaCls}
                          />
                        </FormControl>
                        <FormMessage />
                      </FormItem>
                    )}
                  />
                )}
              </div>

              {canEdit("customInstructions") && (
                <FormField
                  control={form.control}
                  name="customInstructions"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>Indexing instructions</FormLabel>
                      <FormDescription>
                        Standing guidance for how this repository should be documented,
                        appended to the indexer&apos;s playbook. It steers emphasis,
                        audience and vocabulary; it cannot switch off grounding, and
                        every page still cites its real sources.
                      </FormDescription>
                      <FormControl>
                        <Textarea
                          {...field}
                          rows={5}
                          placeholder={
                            "This is a Kotlin Android client. Describe the Compose layer in UI terms, and name the config key each page reads."
                          }
                          className="w-full bg-[hsl(var(--muted))] border-[hsl(var(--border))] rounded-lg p-2.5 resize-none"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {canEdit("mcpServers") && (
                <FormField
                  control={form.control}
                  name="mcpServers"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>External MCP servers</FormLabel>
                      <FormDescription>
                        Extra tools the indexer may call while documenting this
                        repository, in the same JSON shape as an
                        <span className="font-mono"> .mcp.json</span> file. An entry
                        names a process or endpoint the indexing run will reach, so
                        only add servers you trust.
                        {attachedNames.length > 0 ? (
                          <>
                            {" "}
                            Currently attached:{" "}
                            <span className="font-mono">{attachedNames.join(", ")}</span>.
                            Their configuration is not shown — it can contain
                            credentials — so leave this empty to keep it, or paste a
                            complete replacement.
                          </>
                        ) : (
                          " Leave empty to attach none."
                        )}
                      </FormDescription>
                      <FormControl>
                        <Textarea
                          {...field}
                          rows={7}
                          spellCheck={false}
                          placeholder={
                            '{\n  "schema-registry": {\n    "url": "https://registry.example.com/mcp"\n  }\n}'
                          }
                          className={textareaCls}
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {canEdit("desc") && (
                <FormField
                  control={form.control}
                  name="desc"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <FormLabel>Description</FormLabel>
                      <FormControl>
                        <Textarea
                          {...field}
                          rows={2}
                          placeholder="Shown on the wiki gallery card"
                          className="w-full bg-[hsl(var(--muted))] border-[hsl(var(--border))] rounded-lg p-2.5 resize-none"
                        />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              {/* Developer mode — only offered when the SERVER says this project
                  may flip it (i.e. `runtime.developer_mode` is on). */}
              {canEdit("graphOnly") && (
                <FormField
                  control={form.control}
                  name="graphOnly"
                  render={({ field }) => (
                    <FormItem className="space-y-1.5">
                      <label className="flex items-start gap-3 p-3 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/30 cursor-pointer">
                        <span className="text-[hsl(var(--primary))] mt-0.5 shrink-0">
                          <Network className="h-4 w-4" />
                        </span>
                        <span className="flex-1 min-w-0">
                          <span className="block text-sm font-medium">
                            Graph only — skip documentation (no LLM)
                          </span>
                          <span className="block text-2xs text-[hsl(var(--muted-foreground))]">
                            Build the AST code graph only. Turning this on leaves
                            any pages from a previous index in place until the
                            next run replaces them.
                          </span>
                        </span>
                        <FormControl>
                          <Switch
                            checked={field.value}
                            onCheckedChange={field.onChange}
                            aria-label="Graph only — skip documentation (no LLM)"
                            className="mt-0.5 shrink-0"
                          />
                        </FormControl>
                      </label>
                      <FormMessage />
                    </FormItem>
                  )}
                />
              )}

              <CredentialLine credential={credential} />

              {banner && <DialogBanner message={banner} />}

              <DialogFooter className="items-center sm:justify-between gap-3">
                <p className="inline-flex items-center gap-1.5 text-2xs text-[hsl(var(--muted-foreground))]">
                  <RotateCcw className="h-3 w-3 shrink-0" />
                  {descOnlyEdit ? (
                    <>The description updates right away.</>
                  ) : (
                    <>Applies at the next index — saving doesn&apos;t re-index.</>
                  )}
                </p>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="ghost"
                    size="md"
                    onClick={() => onOpenChange(false)}
                    disabled={updateM.isPending}
                  >
                    Cancel
                  </Button>
                  <Button
                    type="submit"
                    variant="primary"
                    size="md"
                    disabled={updateM.isPending || !form.formState.isDirty}
                    leadingIcon={
                      updateM.isPending ? (
                        <Loader2 className={cn("w-4 h-4 animate-spin")} />
                      ) : (
                        <Save className="w-4 h-4" />
                      )
                    }
                  >
                    {updateM.isPending ? "Saving…" : "Save changes"}
                  </Button>
                </div>
              </DialogFooter>
            </form>
          </Form>
        )}
      </DialogContent>
    </Dialog>
  );
}
