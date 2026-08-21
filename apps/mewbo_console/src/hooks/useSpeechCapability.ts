import { useQuery } from "@tanstack/react-query";
import { fetchSpeechCapability } from "../api/speech";

/**
 * Whether the server advertises speech, and therefore whether the console
 * shows any speech control at all.
 *
 * ONE hook and ONE query key for every speech surface (the turn footer's
 * read-aloud button, the composer's mic). TanStack dedupes by key, so a
 * transcript rendering forty turns still issues a single request, and there is
 * a single place to reconcile when the endpoint's final path lands.
 *
 * **Unavailable is the default, and a failure is unavailable.** `retry: false`
 * plus a flag that only a literal `true` can raise means an error, a 404, or a
 * server that has never heard of speech all resolve to "hide the control" —
 * fail-closed, never a broken button offering something the server cannot do.
 * That also makes this safe to ship ahead of the endpoint.
 */
export function useSpeechCapability() {
  const q = useQuery({
    queryKey: ["speech-capability"],
    queryFn: fetchSpeechCapability,
    // Capability changes only when the deployment does, so this outlives the
    // 60s default by a wide margin rather than re-probing on every mount.
    staleTime: 10 * 60_000,
    retry: false,
  });
  return {
    canSynthesize: q.data?.synthesis === true,
    canTranscribe: q.data?.transcription === true,
    /** Upload ceiling for one recording, or `null` while unknown/unpublished. */
    maxAudioBytes: q.data?.maxAudioBytes ?? null,
    /** Character ceiling for one synthesis, or `null` while unknown/unpublished. */
    maxTextChars: q.data?.maxTextChars ?? null,
  };
}
