import { useCallback, useEffect, useRef, useState } from "react";
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
  const [schema, setSchema] = useState<Record<string, unknown> | null>(null);
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const originalRef = useRef<Record<string, unknown> | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [s, c] = await Promise.all([getConfigSchema(), getConfig()]);
      setSchema(s);
      setConfig(c);
      originalRef.current = c;
    } catch (err) {
      const message = logApiError("getConfig", err);
      setError(message);
    } finally {
      setLoading(false);
    }
  }, []);

  const save = useCallback(
    async (formData: Record<string, unknown>): Promise<boolean> => {
      const patch = originalRef.current
        ? shallowDiff(originalRef.current, formData)
        : formData;
      if (!patch) {
        setError(null);
        return true; // nothing to save
      }
      setSaving(true);
      setError(null);
      try {
        const updated = await patchConfig(patch);
        setConfig(updated);
        originalRef.current = updated;
        return true;
      } catch (err) {
        const message = logApiError("patchConfig", err);
        setError(message);
        return false;
      } finally {
        setSaving(false);
      }
    },
    []
  );

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { schema, config, loading, saving, error, save, refresh };
}
