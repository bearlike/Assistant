/**
 * `fetchSpeechCapability` against the REAL capability document.
 *
 * Every other speech test mocks this function wholesale, so none of them touch
 * the parser — and the parser is where a silent, total failure lives. If it
 * misread the document, `canSynthesize` would be `false` on a healthy
 * deployment: no speaker button, no mic, no error anywhere, and every suite
 * still green. A hidden control looks identical to a control that was never
 * built.
 *
 * The payload below is copied from what the server actually builds
 * (`apps/mewbo_api/src/mewbo_api/speech/routes.py`, `SpeechRoutesController.
 * capabilities`) rather than from a description of it, because a fixture
 * written from the client's own beliefs only ever proves the client agrees with
 * itself. **`available` and both caps are NESTED** — the legs carry their
 * configured models, voices and limits — and a parser reading a top-level
 * boolean would return `false` for all of it.
 *
 * The fail-closed rule is the other half: only a literal `true` may show a
 * control, so an error, an absent leg, or a truthy leg with no `available` key
 * all resolve to hidden.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchSpeechCapability } from "../api/speech";

/** The document the deployed namespace returns, verbatim in shape. */
const REAL_PAYLOAD = {
  synthesis: {
    available: true,
    models: [{ id: "supertonic-3", mode: "audio_speech", display_name: "supertonic-3" }],
    voices: [
      "alloy", "ash", "ballad", "coral", "echo", "fable",
      "nova", "onyx", "sage", "shimmer", "verse",
    ],
    formats: ["wav", "flac"],
    defaults: { model: "supertonic-3", voice: "nova", response_format: "wav" },
    limits: { max_text_chars: 2000 },
  },
  transcription: {
    available: true,
    models: [{ id: "nova-3", mode: "audio_transcription", display_name: "nova-3" }],
    defaults: { model: "nova-3" },
    limits: { max_audio_bytes: 10485760 },
  },
  limits: { max_concurrent_calls: 4 },
};

function stubFetch(body: unknown, ok = true) {
  vi.stubGlobal(
    "fetch",
    vi.fn(() =>
      Promise.resolve(
        new Response(JSON.stringify(body), {
          status: ok ? 200 : 503,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("fetchSpeechCapability against the real document", () => {
  it("reads both legs and both caps out of the nested shape", async () => {
    stubFetch(REAL_PAYLOAD);

    const capability = await fetchSpeechCapability();

    expect(capability).toEqual({
      synthesis: true,
      transcription: true,
      maxTextChars: 2000,
      maxAudioBytes: 10485760,
    });
  });

  it("does not mistake the top-level limits block for a leg's own", async () => {
    // `limits.max_concurrent_calls` sits beside the legs, not inside them. A
    // parser reaching for the wrong `limits` would read 4 as a character cap
    // and refuse every response longer than four characters.
    stubFetch({ ...REAL_PAYLOAD, synthesis: { available: true } });

    const capability = await fetchSpeechCapability();

    expect(capability.synthesis).toBe(true);
    expect(capability.maxTextChars).toBeNull();
  });

  it("hides a leg the server reports as unavailable", async () => {
    stubFetch({
      ...REAL_PAYLOAD,
      synthesis: { ...REAL_PAYLOAD.synthesis, available: false },
    });

    const capability = await fetchSpeechCapability();

    expect(capability.synthesis).toBe(false);
    // The cap is still published and still read — a client may want to show it
    // even while the leg is down.
    expect(capability.maxTextChars).toBe(2000);
    expect(capability.transcription).toBe(true);
  });

  it("treats a present-but-unmarked leg as unavailable, not as truthy", async () => {
    // The trap a bare `if (payload.synthesis)` would fall into: a non-empty
    // object is truthy, so a leg that never said `available` would show a
    // control the server cannot serve.
    stubFetch({ synthesis: { models: [], voices: [] }, transcription: null });

    const capability = await fetchSpeechCapability();

    expect(capability.synthesis).toBe(false);
    expect(capability.transcription).toBe(false);
  });

  it("still accepts the bare-boolean form", async () => {
    // Kept deliberately: the mic surface's own suite drives the parser with
    // this shorter shape. Tolerating it costs one comparison and means neither
    // spelling can silently hide a control.
    stubFetch({ synthesis: true, transcription: true });

    const capability = await fetchSpeechCapability();

    expect(capability.synthesis).toBe(true);
    expect(capability.transcription).toBe(true);
    expect(capability.maxTextChars).toBeNull();
  });

  it("rejects a refusal rather than reporting a capability", async () => {
    // A 503 must reach the hook as an error so `retry: false` + the literal-true
    // rule resolve it to hidden, never to a half-read document.
    stubFetch({ error: { code: "speech_unavailable", reason: "not configured" } }, false);

    await expect(fetchSpeechCapability()).rejects.toThrow();
  });
});
