import { useQuery, useQueryClient } from "@tanstack/react-query";
import { listModels } from "../api/client";
import { logApiError } from "../utils/errors";

export function useModels() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["models"], queryFn: listModels });
  return {
    models: q.data?.models ?? [],
    defaultModel: q.data?.default ?? "",
    loading: q.isPending,
    error: q.error ? logApiError("listModels", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["models"] }),
  };
}
