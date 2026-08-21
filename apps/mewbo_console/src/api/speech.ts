/**
 * Speech — synthesis (read a response aloud) and the availability probe that
 * gates every speech control in the console.
 *
 * Shaped like `api/git.ts`: reads `API_BASE` from `client.ts` and spends the
 * shared `httpBase` quartet, so this is not a second fetch wrapper. It sits
 * beside `realClient.ts` rather than inside it because the surface is
 * feature-scoped and grows on its own axis (transcription joins synthesis
 * here), exactly as the git-credential surface does.
 *
 * ⚠️ **The synthesis response is BINARY and its `Content-Type` is a lie.** The
 * gateway answers `audio/mpeg` for every successful call while returning RIFF/
 * WAVE (or FLAC when asked) — measured, never once matching. So nothing here
 * branches on the header: the bytes go into a `Blob` untyped and the browser
 * sniffs the container itself when the blob URL reaches an `<audio>` element.
 * `readJson` cannot serve this path (the body is not JSON), but `readError`
 * still owns the failure path, which is what keeps error messages identical to
 * every other endpoint's.
 *
 * Synthesis is request/response, NOT streaming: the gateway buffers the whole
 * file before sending a byte, so `stream=true` changes nothing and there is no
 * progressive path to build. Cost class: `O(input length)` — roughly 0.5 s for
 * a sentence and 4 s for a paragraph, measured. A caller MUST show a loading
 * state; this is not an interactive-latency call.
 */
import { API_BASE } from "./client";
import {
  apiFetch,
  authHeaders,
  jsonHeaders,
  readError,
  readJson,
  withBase as sharedWithBase,
} from "./httpBase";

// The ONLY place the console names the speech namespace — reconciled here and
// nowhere else. `/api` rather than `/v1`: `/api/<area>` is the dominant RESTX
// registration convention in the API tree, and `/v1/*` is the minority form
// reserved for the streaming/non-CRUD surfaces (draft, structured, wiki).
const SPEECH_BASE = "/api/speech";

function withBase(path: string): string {
  return sharedWithBase(API_BASE, path);
}

/**
 * What the server advertises it can do. Deliberately the minimum both speech
 * surfaces need: a flag each. Model and voice selection is the server's
 * business (it resolves them from config), so neither crosses this boundary.
 */
export interface SpeechCapability {
  /** At least one text-to-speech model is reachable. */
  synthesis: boolean;
  /** At least one speech-to-text model is reachable. */
  transcription: boolean;
  /**
   * Server-published ceiling on one transcription upload, in bytes, or `null`
   * when the server publishes none. Read rather than hardcoded: the cap is a
   * server-side policy number and a client copy of it goes stale silently.
   */
  maxAudioBytes: number | null;
  /**
   * Server-published ceiling on one synthesis request, in characters, or `null`
   * when unpublished. Same rule as `maxAudioBytes` — the number lives in
   * `synthesis.limits.max_text_chars` precisely so no client embeds its own
   * copy, and the server's own 400 names it too.
   */
  maxTextChars: number | null;
}

/**
 * Read one capability leg, accepting BOTH shapes the namespace may answer in.
 *
 * **The server sends the OBJECT form** — each leg carries its configured
 * models, voices, defaults and limits alongside the flag. The bare `true` is
 * tolerated because the mic surface's suite drives the parser with it, and one
 * extra comparison is cheaper than two spellings of "available" disagreeing.
 *
 * Reading only a top-level boolean is the failure this guards: it would return
 * `false` against a perfectly healthy deployment and hide every speech control,
 * with no error anywhere to explain it. Anything that is not a literal `true`
 * is "not available" — absent, null, a string, or a truthy object with no
 * `available` key, which is the one a bare truthiness check gets wrong.
 */
function legAvailable(value: unknown): boolean {
  if (value === true) return true;
  if (value && typeof value === "object") {
    return (value as { available?: unknown }).available === true;
  }
  return false;
}

/**
 * Pull one positive number out of a capability leg's `limits`, or `null`.
 *
 * ONE reader for both legs' caps (`transcription.limits.max_audio_bytes`,
 * `synthesis.limits.max_text_chars`) — two copies of this walk drift the moment
 * one gains a case. A zero or negative value reads as unpublished rather than
 * as a cap that forbids everything, which is the failure a bare truthiness
 * check would ship.
 */
function limitFrom(leg: unknown, key: string): number | null {
  if (!leg || typeof leg !== "object") return null;
  const limits = (leg as { limits?: unknown }).limits;
  if (!limits || typeof limits !== "object") return null;
  const value = (limits as Record<string, unknown>)[key];
  return typeof value === "number" && value > 0 ? value : null;
}

/**
 * Probe what speech the server offers.
 *
 * Reads each flag explicitly rather than spreading the payload: an absent or
 * unrecognised field must mean "not available", never "truthy enough". Cost
 * class: `O(1)`.
 *
 * ⚠️ **Availability is mount state, not health.** The server derives these flags
 * from whether the routes mounted and config named a model — deliberately with
 * no upstream probe, since a probe would put a multi-second gateway call on an
 * interactive path. So `transcription: true` promises a reachable ROUTE, not a
 * working transcription, and a caller must still treat failure as ordinary.
 */
export async function fetchSpeechCapability(): Promise<SpeechCapability> {
  const response = await apiFetch(withBase(`${SPEECH_BASE}/capabilities`), {
    headers: authHeaders(),
  });
  const payload = await readJson<Record<string, unknown>>(response);
  return {
    synthesis: legAvailable(payload.synthesis),
    transcription: legAvailable(payload.transcription),
    maxAudioBytes: limitFrom(payload.transcription, "max_audio_bytes"),
    maxTextChars: limitFrom(payload.synthesis, "max_text_chars"),
  };
}

/**
 * Synthesize `text` and resolve the audio as an untyped `Blob`.
 *
 * `text` is markdown, sent as `chunkForSpeech` built it — this endpoint
 * verbalizes server-side by default (`verbalize` omitted here means `true`),
 * so a table header is announced once and an ordered list keeps its numbers.
 * Never pre-strip before calling this: that is what silenced both features
 * for as long as this console posted already-flattened text.
 *
 * `signal` lets a caller abandon a synthesis that is still in flight — worth
 * having because a paragraph costs seconds, so "I changed my mind" is a real
 * state rather than a theoretical one.
 *
 * Cost class: `O(input length)` on the server; one round trip here.
 */
export async function synthesizeSpeech(text: string, signal?: AbortSignal): Promise<Blob> {
  const response = await apiFetch(withBase(`${SPEECH_BASE}/synthesize`), {
    method: "POST",
    headers: jsonHeaders(),
    body: JSON.stringify({ text }),
    signal,
  });
  if (!response.ok) throw await readError(response);
  return response.blob();
}

/**
 * How long the client waits for a transcription before abandoning it.
 *
 * Deliberately just ABOVE the server's own 30s deadline rather than below it.
 * The server names its own failures (`502 speech_gateway_error` and friends
 * carry a reason), so a client that timed out first would replace a real
 * diagnosis with "it took too long" — the less useful of the two messages. This
 * exists to bound a socket that hangs past every server-side guarantee, not to
 * race the server.
 *
 * The budget is generous relative to the measured cost: transcription returns
 * in well under a second for a short clip. It is sized for a slow network and a
 * long recording, not for the common case.
 */
export const TRANSCRIBE_TIMEOUT_MS = 35_000;

/**
 * Upload one complete recording and resolve its transcript.
 *
 * **The FILENAME EXTENSION is load-bearing, not cosmetic** — the server reads it
 * as the container hint and ignores the part's `Content-Type`. So a caller must
 * derive it from the container `MediaRecorder` actually produced
 * (`useAudioRecorder`'s `fileNameFor`), never from a fixed default: a
 * `.webm`-labelled MP4 degrades transcription silently rather than erroring.
 *
 * One multipart POST of the whole blob, not a chunked or incremental upload. A
 * voice message is seconds of opus, far under the cap, and the transcription
 * route is request/response — there is no partial result to stream back and no
 * size pressure to justify reassembly on the server.
 *
 * Cost class: `O(recording length)`, one round trip.
 */
export async function transcribeSpeech(
  audio: Blob,
  filename: string,
  signal?: AbortSignal,
): Promise<string> {
  const form = new FormData();
  form.append("file", audio, filename);
  const response = await apiFetch(withBase(`${SPEECH_BASE}/transcribe`), {
    method: "POST",
    // `authHeaders()` ONLY — never `jsonHeaders()`, and never a hand-written
    // `multipart/form-data`. The browser sets that header itself together with
    // the boundary token it generated; spelling it here omits the boundary and
    // the server parses zero parts out of a body that looks perfectly valid.
    headers: authHeaders(),
    body: form,
    signal,
  });
  const payload = await readJson<{ text?: unknown }>(response);
  return typeof payload.text === "string" ? payload.text : "";
}
