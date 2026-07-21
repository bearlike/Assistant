/**
 * Custom system instructions — an operator-authored Jinja template (Markdown
 * prose + Jinja control flow) that gets appended to the assistant's system
 * prompt on every session. Backed by `mewbo_core.system_instructions`
 * (`InstructionContext`/`SystemInstructionsDoc`): the template is rendered in
 * a sandboxed Jinja environment against a fixed variable contract, and a
 * failed render never breaks a run (it just injects nothing and records
 * `lastError` on the stored doc).
 *
 * Mirrors the `api/git.ts` seam: typed interfaces over the shared `httpBase`
 * quartet, no third fetch wrapper.
 */
import { API_BASE } from "./client";
import { apiFetch, withBase as sharedWithBase, authHeaders, jsonHeaders, readJson, readError } from "./httpBase";

/**
 * GET /api/system-instructions — the stored singleton document.
 *
 * camelCase, like every other resource here. The API serves an app-owned wire
 * DTO (`SystemInstructionsDto` in
 * `apps/mewbo_api/src/mewbo_api/system_instructions/routes.py`) built field by
 * field FROM core's persistence model rather than dumping that model straight
 * onto the wire, so this interface tracks a contract the API owns — not
 * whatever shape core happens to store today.
 */
export interface SystemInstructionsDoc {
  id?: string;
  template: string;
  enabled: boolean;
  updatedAt: string;
  /** Error from the most recent failed RENDER (in a live session), or null. */
  lastError: string | null;
}

/** Body for PUT /api/system-instructions. */
export interface SystemInstructionsInput {
  template: string;
  enabled: boolean;
}

/**
 * How much a variable's `values` list can be trusted, which is the whole
 * reason it is not just an `enum`:
 *
 * - `closed` — the value a session carries is ALWAYS one of these (`origin`).
 *   A template may branch on them exhaustively.
 * - `known`  — what this DEPLOYMENT can currently produce (installed tools,
 *   configured models, registered projects, the surfaces that stamp a session
 *   today). NOT exhaustive: a new client, plugin or project adds to it, and a
 *   given session only carries the subset its client advertised. A template
 *   must test for a value rather than assume the list is complete.
 */
export type ValuesKind = "closed" | "known";

/**
 * One entry of GET /api/system-instructions/variables — generated from the
 * backend's `InstructionContext` model, so this is the ONLY source for the
 * variable reference table. Never hand-author this list in the frontend, and
 * never hardcode a copy of one of these lists in a component (the preview
 * surface tabs are derived from `surface`'s `values` for exactly this reason).
 */
export interface SystemInstructionsVariable {
  name: string;
  type: string;
  description: string;
  /** Candidate values, or null when the field has no enumerable source. */
  values?: string[] | null;
  /** How exhaustive `values` is. Null whenever `values` is null. */
  valuesKind?: ValuesKind | null;
  /** One sentence on where `values` came from and what caveat it carries. */
  valuesNote?: string | null;
}

/** Body for POST /api/system-instructions/preview. Both fields optional. */
export interface SystemInstructionsPreviewInput {
  template?: string;
  context?: Record<string, unknown>;
}

/**
 * POST /api/system-instructions/preview response. Always a 200 — a template
 * that fails to render comes back as `error`, never a thrown 500, because the
 * whole point of preview is to let the operator see what happens to a broken
 * template before it reaches a live run.
 */
export interface SystemInstructionsPreviewResult {
  rendered: string;
  error: string | null;
}

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/** GET /api/system-instructions */
export async function getSystemInstructions(
  signal?: AbortSignal
): Promise<SystemInstructionsDoc> {
  const response = await apiFetch(withBase("/api/system-instructions"), {
    headers: authHeaders(),
    signal,
  });
  return readJson<SystemInstructionsDoc>(response);
}

/**
 * PUT /api/system-instructions — 400 with a Jinja compile-error message when
 * the template doesn't parse. Returns void (not the updated doc): callers
 * invalidate the query and re-fetch, so the cache is always seeded from a
 * real GET rather than an assumed echo of what was sent.
 */
export async function putSystemInstructions(
  input: SystemInstructionsInput,
  signal?: AbortSignal
): Promise<void> {
  const response = await apiFetch(withBase("/api/system-instructions"), {
    method: "PUT",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
    signal,
  });
  if (!response.ok) throw await readError(response);
}

/**
 * POST /api/system-instructions/preview — render the given (or currently
 * saved, if `template` is omitted) template against a sample context.
 */
export async function previewSystemInstructions(
  input: SystemInstructionsPreviewInput,
  signal?: AbortSignal
): Promise<SystemInstructionsPreviewResult> {
  const response = await apiFetch(withBase("/api/system-instructions/preview"), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify(input),
    signal,
  });
  return readJson<SystemInstructionsPreviewResult>(response);
}

/** GET /api/system-instructions/variables — the variable reference table. */
export async function listSystemInstructionsVariables(
  signal?: AbortSignal
): Promise<SystemInstructionsVariable[]> {
  const response = await apiFetch(withBase("/api/system-instructions/variables"), {
    headers: authHeaders(),
    signal,
  });
  const data = await readJson<
    { variables?: SystemInstructionsVariable[] } | SystemInstructionsVariable[]
  >(response);
  return Array.isArray(data) ? data : (data.variables ?? []);
}
