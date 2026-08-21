// THE console's capability ids — the TypeScript mirror of the python registry in
// `packages/mewbo_core/src/mewbo_core/capabilities.py`.
//
// A capability is a string the client and the server must agree on literally.
// There is no capability-discovery endpoint and no codegen step: this file is a
// HAND-MIRROR, pinned by `src/__tests__/capabilityRegistryAlignment.test.ts`,
// which parses these literals out of this file's source and fails if any of them
// is absent from the python registry. That is the same shape `agentStatus.ts`
// uses against `hypervisor.py`'s `AgentStatus`.
//
// Spell an id ONCE, here. `realClient.ts` and `apps.ts` both send the
// `X-Mewbo-Capabilities` header and both used to hardcode their own copies.
//
// **Advertising a capability and answering it are ONE decision.** Each id below
// asserts that this client can SERVICE the feature — render the card, play the
// audio, run the recorder. A surface that advertises without the answering half
// lets a run spend a step producing something nobody can read, so the comment on
// each id names where its answering half lives.

/** The widget-builder plugin (chat `widget_ready` cards). Answered by `StliteWidgetPanel`. */
export const WIDGET_CAPABILITY_ID =
  (import.meta.env.VITE_WIDGET_CAPABILITY_ID as string | undefined) || "stlite";

/** The Mewbo Apps sub-product. Answered by `components/apps/`. */
export const APPS_CAPABILITY_ID =
  (import.meta.env.VITE_APPS_CAPABILITY_ID as string | undefined) || "apps";

/** Core's ask-user tool gate. Answered by the question card + its answer POST. */
export const ASK_USER_CAPABILITY_ID = "ask_user";

/** Core's `present_ui` gate. Answered by the allowlist renderer in `components/generative-ui/`. */
export const GENERATIVE_UI_CAPABILITY_ID = "generative_ui";

/** This client can PLAY synthesized audio. Answered by `utils/speechPlayback.ts` + `SpeakButton`. */
export const SPEECH_PLAYBACK_CAPABILITY_ID = "speech_playback";

/** This client can RECORD microphone audio. Answered by `hooks/useAudioRecorder.ts`. */
export const SPEECH_CAPTURE_CAPABILITY_ID = "speech_capture";

/**
 * Render capability ids as an `X-Mewbo-Capabilities` header value.
 *
 * Mirrors core's `serialize_capabilities`: sorted and deduped, so the header a
 * client sends round-trips through the server's parser unchanged. The comma is
 * spelled here and nowhere else on this side.
 */
export function serializeCapabilities(ids: readonly string[]): string {
  return [...new Set(ids.map((id) => id.trim()).filter(Boolean))].sort().join(",");
}

/**
 * Everything the ordinary console session client advertises.
 *
 * Deliberately NOT every id in the python registry: `wiki`/`scg` are
 * session-owned substrate capabilities the server derives, and `device_control`
 * belongs to a client with a screen to drive. This is what THIS surface can
 * service.
 */
export const CLIENT_CAPABILITIES = serializeCapabilities([
  WIDGET_CAPABILITY_ID,
  APPS_CAPABILITY_ID,
  ASK_USER_CAPABILITY_ID,
  GENERATIVE_UI_CAPABILITY_ID,
  SPEECH_PLAYBACK_CAPABILITY_ID,
  SPEECH_CAPTURE_CAPABILITY_ID,
]);
