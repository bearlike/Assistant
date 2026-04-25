import { useQuery } from "@tanstack/react-query";
import { fetchCommands } from "../api/client";
import { CommandSpec } from "../types";

/**
 * Fetch the server-side command registry once per session.
 *
 * The registry rarely changes at runtime (handlers are deployed with the
 * server) so we set ``staleTime: Infinity`` and rely on a hard refresh to
 * pick up new commands.
 */
export function useCommands(): {
  commands: CommandSpec[];
  loading: boolean;
} {
  const q = useQuery<CommandSpec[]>({
    queryKey: ["commands"],
    queryFn: fetchCommands,
    staleTime: Infinity,
    retry: 1,
  });
  return {
    commands: q.data ?? [],
    loading: q.isPending,
  };
}
