/**
 * Model IDs containing any of these substrings are exposed by the API base
 * (OpenAI-compatible endpoints, etc.) but are NOT usable for chat or agent
 * loops — they're speech-to-text, transcription, or embedding endpoints.
 *
 * Matching is case-insensitive substring. To extend: append to the array.
 */
const UNSUPPORTED_SUBSTRINGS = ['whisper', 'embedding', 'embed'];

export function isUnsupportedModel(modelId: string): boolean {
  const lower = modelId.toLowerCase();
  return UNSUPPORTED_SUBSTRINGS.some((s) => lower.includes(s));
}
