import { useCallback, useEffect, useState } from "react";
import {
  clearNotifications,
  dismissNotification,
  listNotifications
} from "../api/client";
import { NotificationItem } from "../types";
import { logApiError } from "../utils/errors";

export function useNotifications() {
  const [notifications, setNotifications] = useState<NotificationItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listNotifications();
      setNotifications(data);
    } catch (err) {
      const message = logApiError("listNotifications", err);
      setError(message);
      setNotifications([]);
    } finally {
      setLoading(false);
    }
  }, []);

  const dismiss = useCallback(async (id: string) => {
    try {
      await dismissNotification([id]);
      setNotifications((prev) => prev.filter((n) => n.id !== id));
    } catch (err) {
      const message = logApiError("dismissNotification", err);
      setError(message);
    }
  }, []);

  const clearAll = useCallback(async () => {
    try {
      await clearNotifications(true);
      setNotifications([]);
    } catch (err) {
      const message = logApiError("clearNotifications", err);
      setError(message);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return {
    notifications,
    loading,
    error,
    refresh,
    dismiss,
    clearAll
  };
}
