/**
 * Pure form model for `ProjectSettingsDialog.tsx` — schema, wire↔form field
 * mapping, and the dirty-subset patch builder. No JSX, no React: this is the
 * part of the dialog that's cheap to unit-test and has nothing to do with
 * rendering.
 *
 * See `ProjectSettingsDialog.tsx`'s header comment for the non-obvious rules
 * this module encodes (the `ref`-vs-`branch` field-name trap, `editable`
 * fail-closed gating, dirty-subset PATCH, `desc`'s immediate-write exception).
 */

import { z } from "zod";

import type {
  ProjectSettings,
  ProjectSettingsField,
  ProjectSettingsPatch,
} from "./api/types";
import { isCatalogSettings } from "./api/types";

// ── Form model ────────────────────────────────────────────────────────────

export const settingsSchema = z.object({
  model: z.string().trim(),
  /** Newline-separated in the form; `string[] | null` on the wire — the same
   *  textarea-shaped representation `dirs`/`files` use, chosen because rhf
   *  reports a plain string dirty reliably where an array field reports a
   *  per-index boolean array that reads truthy even when emptied. */
  fallbackModels: z.string(),
  /** "" = the repo's default branch (PATCHed as an explicit null). Named
   *  `branch`, not `ref`: a form field called `ref` collides with the `ref` key
   *  on react-hook-form's own field object, and the control silently stops
   *  reporting itself dirty (Save never wakes up). The WIRE key stays `ref`. */
  branch: z.string().trim(),
  depth: z.enum(["comprehensive", "concise"]),
  language: z.string().trim().min(1, "Pick a language"),
  filterMode: z.enum(["exclude", "include"]),
  /** Newline-separated in the form; string[] on the wire. */
  dirs: z.string(),
  files: z.string(),
  graphOnly: z.boolean(),
  desc: z.string().trim().max(240, "Keep the description under 240 characters"),
});

export type SettingsValues = z.infer<typeof settingsSchema>;

/** Wire field → form field. Used to pin server field errors to their input. */
export const FORM_FIELD_BY_WIRE: Record<ProjectSettingsField, keyof SettingsValues> = {
  model: "model",
  fallbackModels: "fallbackModels",
  ref: "branch",
  depth: "depth",
  language: "language",
  filterMode: "filterMode",
  dirs: "dirs",
  files: "files",
  graphOnly: "graphOnly",
  desc: "desc",
};

/** Resolve a server-sent field-error key to its input. Returns undefined for a
 *  key this form doesn't render (`repoUrl`/`platform` are identity — the server
 *  flags them editable, we refuse to offer them), so the error falls through to
 *  the dialog banner rather than being silently dropped. */
export function formFieldFor(wire: string): keyof SettingsValues | undefined {
  return (FORM_FIELD_BY_WIRE as Record<string, keyof SettingsValues | undefined>)[wire];
}

/**
 * Every field that only takes effect at the NEXT index. `desc` is the sole
 * exception — the server writes it straight through to the Project snapshot (and
 * finalize reads the override back, so a reindex won't clobber it), which is why
 * it's the one edit that shows up immediately.
 */
export const INDEX_TIME_FIELDS: Array<keyof ProjectSettingsPatch> = [
  "model",
  "fallbackModels",
  "ref",
  "depth",
  "language",
  "filterMode",
  "dirs",
  "files",
  "graphOnly",
];

export const needsReindex = (patch: ProjectSettingsPatch): boolean =>
  INDEX_TIME_FIELDS.some((k) => k in patch);

/** Inert baseline — replaced by `form.reset(seedFrom(dto))` the moment the
 *  settings land. Nothing is rendered before that (the dialog shows a spinner),
 *  so these values are never shown to anyone. */
export const EMPTY_FORM: SettingsValues = {
  model: "",
  fallbackModels: "",
  branch: "",
  depth: "comprehensive",
  language: "en",
  filterMode: "exclude",
  dirs: "",
  files: "",
  graphOnly: false,
  desc: "",
};

/** Seed the form from the settings DTO. Catalog payloads carry only the
 *  display fields — the rest fall back to inert defaults and are never shown
 *  (the server's `editable` map doesn't flag them). */
export function seedFrom(s: ProjectSettings): SettingsValues {
  const git = isCatalogSettings(s) ? null : s;
  return {
    ...EMPTY_FORM,
    model: s.model ?? "",
    fallbackModels: (s.fallbackModels ?? []).join("\n"),
    branch: git?.ref ?? "",
    depth: git?.depth ?? EMPTY_FORM.depth,
    language: git?.language ?? EMPTY_FORM.language,
    filterMode: git?.filterMode ?? EMPTY_FORM.filterMode,
    dirs: (git?.dirs ?? []).join("\n"),
    files: (git?.files ?? []).join("\n"),
    graphOnly: git?.graphOnly ?? false,
    desc: s.desc ?? "",
  };
}

/** Newline-separated textarea value → trimmed, non-empty lines. Shared with
 *  `ConfigureWizard.tsx`'s scope step, which parses the same dirs/files shape. */
export const splitLines = (text: string): string[] =>
  text.split("\n").map((l) => l.trim()).filter(Boolean);

/**
 * The changed subset, in wire shape. rhf marks a field dirty by comparing it to
 * the seeded defaults, so this is exactly "what the user touched" — an
 * untouched field is absent from the body, not sent as its current value.
 */
export function buildPatch(
  values: SettingsValues,
  dirty: Partial<Record<keyof SettingsValues, boolean | undefined>>,
): ProjectSettingsPatch {
  const patch: ProjectSettingsPatch = {};
  if (dirty.model) patch.model = values.model.trim();
  // An emptied ladder is an explicit "no fallback" (null), distinct from
  // omitting the key, which leaves the stored ladder alone.
  if (dirty.fallbackModels) {
    const ladder = splitLines(values.fallbackModels);
    patch.fallbackModels = ladder.length > 0 ? ladder : null;
  }
  // An emptied branch means "back to the repo's default" — an explicit null,
  // distinct from omitting `ref` (which leaves the pin untouched).
  if (dirty.branch) patch.ref = values.branch.trim() || null;
  if (dirty.depth) patch.depth = values.depth;
  if (dirty.language) patch.language = values.language;
  if (dirty.filterMode) patch.filterMode = values.filterMode;
  if (dirty.dirs) patch.dirs = splitLines(values.dirs);
  if (dirty.files) patch.files = splitLines(values.files);
  if (dirty.graphOnly) patch.graphOnly = values.graphOnly;
  if (dirty.desc) patch.desc = values.desc.trim();
  return patch;
}
