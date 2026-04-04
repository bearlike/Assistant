import { useCallback, useEffect, useState } from "react";
import { listModels, invalidateCache, peekCache } from "../api/client";
import { ModelInfo } from "../api/contracts";
import { logApiError } from "../utils/errors";

function readCached(): ModelInfo | undefined {
  return peekCache<ModelInfo>('models') ?? undefined;
}

export function useModels() {
  const [models, setModels] = useState<string[]>(readCached()?.models ?? []);
  const [defaultModel, setDefaultModel] = useState<string>(readCached()?.default ?? "");
  const [loading, setLoading] = useState(() => !readCached());
  const [error, setError] = useState<string | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  useEffect(() => {
    let mounted = true;
    const stale = readCached();
    if (stale) {
      setModels(stale.models);
      setDefaultModel(stale.default);
    }
    setLoading(!stale);
    setError(null);
    listModels()
      .then((info) => {
        if (mounted) {
          setModels(info.models);
          setDefaultModel(info.default);
        }
      })
      .catch((err) => {
        if (mounted) {
          const message = logApiError("listModels", err);
          setError(message);
          if (!stale) setModels([]);
        }
      })
      .finally(() => { if (mounted) setLoading(false); });
    return () => { mounted = false; };
  }, [fetchKey]);

  const refresh = useCallback(() => {
    invalidateCache("models");
    setFetchKey((k) => k + 1);
  }, []);

  return { models, defaultModel, loading, error, refresh };
}
