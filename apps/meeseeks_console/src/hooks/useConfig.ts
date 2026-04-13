import { useEffect, useRef } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { getConfig, getConfigSchema, patchConfig } from "../api/client";
import { logApiError } from "../utils/errors";

export type ConfigState = {
  schema: Record<string, unknown> | null;
  config: Record<string, unknown> | null;
  loading: boolean;
  saving: boolean;
  error: string | null;
  save: (formData: Record<string, unknown>) => Promise<boolean>;
  refresh: () => Promise<void>;
};

/** Compute a shallow-recursive diff: only keys whose values changed. */
function shallowDiff(
  original: Record<string, unknown>,
  updated: Record<string, unknown>
): Record<string, unknown> | null {
  const diff: Record<string, unknown> = {};
  let hasChange = false;
  for (const key of Object.keys(updated)) {
    const origVal = original[key];
    const newVal = updated[key];
    if (
      typeof origVal === "object" &&
      origVal !== null &&
      !Array.isArray(origVal) &&
      typeof newVal === "object" &&
      newVal !== null &&
      !Array.isArray(newVal)
    ) {
      const nested = shallowDiff(
        origVal as Record<string, unknown>,
        newVal as Record<string, unknown>
      );
      if (nested) {
        diff[key] = nested;
        hasChange = true;
      }
    } else if (JSON.stringify(origVal) !== JSON.stringify(newVal)) {
      diff[key] = newVal;
      hasChange = true;
    }
  }
  return hasChange ? diff : null;
}

export function useConfig(): ConfigState {
  const qc = useQueryClient();
  const originalRef = useRef<Record<string, unknown> | null>(null);

  const schemaQ = useQuery({
    queryKey: ["config-schema"],
    queryFn: getConfigSchema,
    staleTime: Infinity,
  });
  const configQ = useQuery({ queryKey: ["config"], queryFn: getConfig });
  useEffect(() => {
    if (configQ.data) {
      originalRef.current = configQ.data;
    }
  }, [configQ.data]);
  const patchM = useMutation({
    mutationFn: (patch: Record<string, unknown>) => patchConfig(patch),
    onSuccess: (updated) => {
      originalRef.current = updated;
      qc.setQueryData(["config"], updated);
    },
  });

  const save = async (formData: Record<string, unknown>): Promise<boolean> => {
    const patch = originalRef.current
      ? shallowDiff(originalRef.current, formData)
      : formData;
    if (!patch) {
      return true;
    }
    try {
      await patchM.mutateAsync(patch);
      return true;
    } catch {
      return false;
    }
  };

  const refresh = async () => {
    await Promise.all([
      qc.invalidateQueries({ queryKey: ["config"] }),
      qc.invalidateQueries({ queryKey: ["config-schema"] }),
    ]);
  };

  const errSource = schemaQ.error || configQ.error || patchM.error;
  return {
    schema: schemaQ.data ?? null,
    config: configQ.data ?? null,
    loading: schemaQ.isPending || configQ.isPending,
    saving: patchM.isPending,
    error: errSource ? logApiError("config", errSource) : null,
    save,
    refresh,
  };
}
