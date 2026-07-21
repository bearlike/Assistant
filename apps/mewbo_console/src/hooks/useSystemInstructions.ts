/**
 * TanStack Query hooks for the custom system-instructions Settings pane.
 *
 * Four surfaces: the stored doc (read + save), the variable reference (read,
 * long `staleTime` since it's derived from a backend Pydantic model that
 * doesn't change within a session), and preview (an on-demand render, so a
 * mutation rather than a cached query — the result depends on whatever the
 * operator is currently typing, not server state worth caching by key).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  getSystemInstructions,
  listSystemInstructionsVariables,
  previewSystemInstructions,
  putSystemInstructions,
  type SystemInstructionsInput,
  type SystemInstructionsPreviewInput,
  type SystemInstructionsVariable,
} from "../api/systemInstructions";
import { logApiError } from "../utils/errors";

export const SYSTEM_INSTRUCTIONS_KEY = ["system-instructions"] as const;
const VARIABLES_KEY = [...SYSTEM_INSTRUCTIONS_KEY, "variables"] as const;

/** GET /api/system-instructions */
export function useSystemInstructions() {
  const q = useQuery({
    queryKey: SYSTEM_INSTRUCTIONS_KEY,
    queryFn: ({ signal }) => getSystemInstructions(signal),
    staleTime: 30_000,
  });
  return {
    doc: q.data,
    loading: q.isLoading,
    error: q.error ? logApiError("getSystemInstructions", q.error) : null,
  };
}

/**
 * Referentially stable stand-in for "no variables yet".
 *
 * Load-bearing, not a micro-optimization: the pane feeds `variables` to a
 * `useMemo` that builds the CodeMirror extension set. A fresh `[]` literal on
 * every render would be a new identity each time, so the editor would be
 * reconfigured on every render while the query is still in flight.
 */
const NO_VARIABLES: SystemInstructionsVariable[] = [];

/**
 * GET /api/system-instructions/variables — the operator-facing reference table
 * AND the source of the editor's autocomplete and the preview surface tabs.
 *
 * `staleTime` is one minute rather than the five it once was. The payload used
 * to be a pure function of a backend Pydantic model (identical for the life of
 * a deployment), but it now also reports what this deployment actually has:
 * bound tools, configured models, registered projects, advertised
 * capabilities. Installing a plugin or adding a project changes it, so a long
 * cache would quietly serve an operator a list that no longer matches the
 * system they are writing a template for. One minute means the next mount
 * after such a change picks it up. Deliberately no polling: nothing here
 * changes mid-edit, and a refetch that swapped the completion list out from
 * under someone mid-keystroke would be worse than a slightly stale list.
 */
export function useSystemInstructionsVariables() {
  const q = useQuery({
    queryKey: VARIABLES_KEY,
    queryFn: ({ signal }) => listSystemInstructionsVariables(signal),
    staleTime: 60_000,
  });
  return {
    variables: q.data ?? NO_VARIABLES,
    loading: q.isLoading,
    error: q.error ? logApiError("listSystemInstructionsVariables", q.error) : null,
  };
}

/** PUT /api/system-instructions */
export function useSaveSystemInstructions() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: SystemInstructionsInput) => putSystemInstructions(input),
    onSuccess: () => qc.invalidateQueries({ queryKey: SYSTEM_INSTRUCTIONS_KEY }),
  });
}

/** POST /api/system-instructions/preview */
export function usePreviewSystemInstructions() {
  return useMutation({
    mutationFn: (input: SystemInstructionsPreviewInput) => previewSystemInstructions(input),
    onError: (err) => logApiError("previewSystemInstructions", err),
  });
}
