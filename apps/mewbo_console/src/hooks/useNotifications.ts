import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  clearNotifications,
  dismissNotification,
  listNotifications,
} from "../api/client";
import { logApiError } from "../utils/errors";

export function useNotifications() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["notifications"],
    queryFn: listNotifications,
    refetchInterval: 30_000,
  });
  const dismissM = useMutation({
    mutationFn: (id: string) => dismissNotification([id]),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
  const clearM = useMutation({
    mutationFn: () => clearNotifications(true),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
  });
  return {
    notifications: q.data ?? [],
    loading: q.isPending,
    error: q.error ? logApiError("listNotifications", q.error) : null,
    refresh: () => qc.invalidateQueries({ queryKey: ["notifications"] }),
    dismiss: (id: string) => dismissM.mutate(id),
    clearAll: () => clearM.mutate(),
  };
}
