/**
 * The chunked read pipeline, driven through the REAL `AssistantTurnFooter`.
 *
 * `speakButton.test.tsx` next door pins the button's own three states against a
 * short response. This file pins what happens to a LONG one, which is a
 * different machine: several synthesis requests, playback advancing between
 * them, and a stop that has to reach requests that have not been issued yet.
 *
 * The properties pinned here, in the order they matter:
 *
 * 1. **A stop stops spending.** Abandoning a read must issue no further
 *    synthesis requests and must abort the one in flight. This is the whole
 *    reason the pipeline is sequential rather than a fan-out, and it is
 *    asserted on the FETCH COUNT — a button that merely goes quiet while the
 *    remaining eight paragraphs keep synthesizing would pass every visual check.
 * 2. **At most one synthesis runs ahead of playback.** The server allows four
 *    in-flight speech calls across ALL users, so a ten-chunk parallel burst
 *    would 503 its own tail and starve every other caller.
 * 3. **Playback advances on its own**, and the button stays a stop control for
 *    the whole read rather than flickering back to a wait per chunk.
 * 4. **A chunk that fails is reported.** A read that dies three paragraphs in
 *    is otherwise indistinguishable from one that finished: the audio just
 *    stops.
 *
 * Only the HTTP module and the toast sink are stubbed. The chunker, the button,
 * the footer, the capability hook, the reader and the playback singleton are
 * all real, so the chunk boundaries under test are the ones production computes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

vi.mock("../api/speech", () => ({
  fetchSpeechCapability: vi.fn(),
  synthesizeSpeech: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { error: vi.fn(), success: vi.fn() } }));

import * as speechApi from "../api/speech";
import { toast } from "sonner";
import { AssistantTurnFooter } from "../components/ConversationTimeline";
import { chunkForSpeech } from "../utils/speechChunks";
import { speechReader } from "../utils/speechPlayback";
import type { TurnMeta } from "../types";

const TURN: TurnMeta = {
  id: "turn-1",
  events: [{ ts: "2026-01-02T03:04:05Z", type: "completion", payload: {} }],
  files: [],
  model: "claude-sonnet-5",
};

/** The server's published ceiling, mirrored by the capability mock below. */
const SERVER_CAP = 2000;

/**
 * Ten paragraphs of ordinary prose — the response the owner described, and the
 * one the endpoint's 2000-character cap used to refuse outright.
 */
const TEN_PARAGRAPHS = Array.from({ length: 10 }, (_, p) =>
  Array.from(
    { length: 5 },
    (_, s) =>
      `Paragraph ${p} sentence ${s} explains one more part of the answer in ` +
      `enough words to be worth hearing read aloud.`,
  ).join(" "),
).join("\n\n");

function renderFooter(responseText: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return render(
    <AssistantTurnFooter
      turn={TURN}
      responseText={responseText}
      onShowTrace={vi.fn()}
      onOpenFiles={vi.fn()}
    />,
    { wrapper },
  );
}

/** One synthesis request this test controls the fate of. */
interface Gate {
  text: string;
  /**
   * The signal the reader passed, which must always be `undefined` — the
   * sequencer deliberately hands the transport no way to abort. Kept on the
   * gate so a test can assert that rather than infer it.
   */
  signal: AbortSignal | undefined;
  settled: boolean;
  resolve: (blob: Blob) => void;
  reject: (error: unknown) => void;
}

const capability = vi.mocked(speechApi.fetchSpeechCapability);
const synthesize = vi.mocked(speechApi.synthesizeSpeech);

/** Every synthesis request issued, in order, still awaiting its outcome. */
let gates: Gate[] = [];
/** Every `<audio>` the player built, and every one it actually started. */
let audioCreated: HTMLAudioElement[] = [];
let audioPlayed: HTMLAudioElement[] = [];

/** Let queued microtasks and the reader's own deferred advance run. */
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** Resolve one outstanding request with a distinct blob. */
function deliver(index: number) {
  gates[index].settled = true;
  gates[index].resolve(new Blob([`audio-${index}`]));
}

/** Fire `ended` on the clip currently playing, as the browser would. */
function endCurrentClip() {
  audioPlayed[audioPlayed.length - 1].dispatchEvent(new Event("ended"));
}

beforeEach(() => {
  vi.clearAllMocks();
  gates = [];
  audioCreated = [];
  audioPlayed = [];
  capability.mockResolvedValue({
    synthesis: true,
    transcription: false,
    maxAudioBytes: null,
    maxTextChars: SERVER_CAP,
  });
  synthesize.mockImplementation(
    (text: string, signal?: AbortSignal) =>
      new Promise<Blob>((resolve, reject) => {
        gates.push({ text, signal, settled: false, resolve, reject });
      }),
  );
  const RealAudio = window.Audio;
  vi.stubGlobal(
    "Audio",
    class CapturedAudio extends RealAudio {
      constructor(src?: string) {
        super(src);
        audioCreated.push(this);
        // ⚠️ Which element PLAYED cannot be captured by overriding `play()`
        // here. jsdom's `Audio` is a legacy factory that builds the element
        // through `document.createElement` and returns it, so `this` is a plain
        // `HTMLAudioElement` and a subclass method never reaches the instance —
        // the constructor body still runs, which is why the created-list works.
        // `setupTests.ts`'s `play` stub dispatches a real `play` event, so the
        // event is the observable that survives the factory.
        this.addEventListener("play", () => audioPlayed.push(this));
      }
    },
  );
});

afterEach(() => {
  speechReader.stop();
  vi.unstubAllGlobals();
  cleanup();
});

/** Click read-aloud, deliver the first chunk, and land in the playing state. */
async function startReading(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Read response aloud" }));
  await screen.findByRole("button", { name: "Preparing audio" });
  await waitFor(() => expect(gates).toHaveLength(1));
  deliver(0);
  await screen.findByRole("button", { name: "Stop reading" });
}

describe("reading a long response aloud", () => {
  it("splits it into several requests rather than refusing it for length", async () => {
    // The premise of every other case here, asserted rather than assumed: this
    // corpus really is past the cap, and really does chunk.
    expect(TEN_PARAGRAPHS.length).toBeGreaterThan(SERVER_CAP);
    const chunks = chunkForSpeech(TEN_PARAGRAPHS, { limitChars: SERVER_CAP });
    expect(chunks.length).toBeGreaterThan(4);

    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);

    // The first request carried the first chunk, not the whole response — which
    // the server would have refused with a 400.
    expect(gates[0].text).toBe(chunks[0]);
    expect(gates[0].text.length).toBeLessThan(SERVER_CAP);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("reads the WHOLE response, in order, and returns to idle at the end", async () => {
    // The replacement for the refusal this feature removed. `speakButton.test.tsx`
    // used to pin "an over-long response is refused up front, naming both
    // numbers"; over-long is now split instead, so the requirement that
    // supersedes it is that nothing is DROPPED on the way — a read that quietly
    // stops after chunk 2 would satisfy every other test in this file.
    const expected = chunkForSpeech(TEN_PARAGRAPHS, { limitChars: SERVER_CAP });
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);

    // Drive the read to its end, one chunk at a time. Bounded so a pipeline
    // that never terminates fails here rather than hanging the suite.
    for (let step = 0; step < expected.length * 2; step++) {
      if (screen.queryByRole("button", { name: "Read response aloud" })) break;
      const pending = gates.find((gate) => !gate.settled);
      if (pending) deliver(gates.indexOf(pending));
      await settle();
      if (audioPlayed.length > 0) endCurrentClip();
      await settle();
    }

    await screen.findByRole("button", { name: "Read response aloud" });
    expect(gates.map((gate) => gate.text)).toEqual(expected);
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("keeps exactly one synthesis in flight ahead of playback", async () => {
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);

    // Chunk 1 is playing and chunk 2 is in flight. Nothing beyond it has been
    // asked for: a fan-out would have issued every chunk by now and taken the
    // server's whole four-wide budget with one click.
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(2));
    await settle();
    expect(synthesize).toHaveBeenCalledTimes(2);

    // Advance one chunk; the window slides rather than widening.
    deliver(1);
    endCurrentClip();
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(3));
    await settle();
    expect(synthesize).toHaveBeenCalledTimes(3);
  });

  it("advances through the chunks on its own, staying a stop control throughout", async () => {
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    const firstClip = audioPlayed[audioPlayed.length - 1];

    await waitFor(() => expect(gates).toHaveLength(2));
    deliver(1);
    endCurrentClip();

    // A second clip started, and the button never went back to "Preparing
    // audio" — the wait is paid once, at the head of the read.
    await waitFor(() => expect(audioPlayed).toHaveLength(2));
    expect(audioPlayed[1]).not.toBe(firstClip);
    expect(screen.getByRole("button", { name: "Stop reading" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Preparing audio" })).toBeNull();
  });

  it("hands the next clip to the player before the current one ends", async () => {
    // Gaplessness in the only form a jsdom test can witness: the element for
    // chunk 2 exists and has been prepared WHILE chunk 1 is still playing, so
    // the gap between clips does not also contain building it.
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);

    await waitFor(() => expect(gates).toHaveLength(2));
    deliver(1);
    await waitFor(() => expect(audioCreated).toHaveLength(2));
    const preloaded = audioCreated[1];
    expect(audioPlayed).toHaveLength(1);

    endCurrentClip();

    // The very element that was preloaded is the one that plays — not a second
    // element built at the seam.
    await waitFor(() => expect(audioPlayed).toHaveLength(2));
    expect(audioPlayed[1]).toBe(preloaded);
  });
});

describe("stopping a long read", () => {
  it("issues no further synthesis requests", async () => {
    // THE requirement. A stop two seconds into a ten-paragraph read must cost
    // two seconds of synthesis, not ten paragraphs of it.
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("button", { name: "Stop reading" }));

    await screen.findByRole("button", { name: "Read response aloud" });
    // Nothing new is asked for, now or once the microtask queue drains.
    await settle();
    expect(synthesize).toHaveBeenCalledTimes(2);

    // The read paid for a fraction of the response, which is the point.
    const requested = gates.reduce((total, gate) => total + gate.text.length, 0);
    expect(requested).toBeLessThan(TEN_PARAGRAPHS.length / 2);
  });

  it("lets the in-flight chunk finish and discards its audio, rather than aborting it", async () => {
    // ⚠️ The counter-intuitive half, and the reason it is pinned: aborting does
    // NOT stop the backend. The abandoned synthesis keeps holding one of the
    // TTS backend's two shared parallel slots, and the next synthesis by ANY
    // caller then measured 14.89 s against a 1.09 s control. Draining costs the
    // same compute and frees the slot on schedule.
    //
    // Two assertions, because either alone is passable by the wrong code: no
    // signal is ever handed to the transport (so nothing CAN be aborted), and
    // the bytes that do arrive are thrown away instead of played.
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(2));
    expect(gates[1].signal).toBeUndefined();

    await user.click(screen.getByRole("button", { name: "Stop reading" }));
    deliver(1);
    await settle();

    // The chunk that was in flight resolved, and nothing played it.
    expect(audioPlayed).toHaveLength(1);
    expect(synthesize).toHaveBeenCalledTimes(2);
    expect(screen.getByRole("button", { name: "Read response aloud" })).toBeInTheDocument();
  });

  it("never requests chunk 3 when stopped while chunk 1 is playing", async () => {
    // The requirement stated the way it is felt, one chunk further in: chunk 2
    // is in flight when the stop lands, and resolving it late must not pull
    // chunk 3 in behind it.
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(gates).toHaveLength(2));
    deliver(1);
    endCurrentClip();
    // Chunk 2 is now playing and chunk 3 is in flight.
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(3));

    await user.click(screen.getByRole("button", { name: "Stop reading" }));
    deliver(2);
    await settle();

    expect(synthesize).toHaveBeenCalledTimes(3);
    expect(audioPlayed).toHaveLength(2);
  });

  it("does not wedge the button when the drained request rejects afterwards", async () => {
    // A request still in flight at stop time may fail on its own afterwards.
    // Nobody is listening for it any more, so it must reach neither the toast
    // nor the state, and must not leave the button unusable.
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(synthesize).toHaveBeenCalledTimes(2));

    await user.click(screen.getByRole("button", { name: "Stop reading" }));
    gates[1].reject(new Error("the gateway gave up"));
    await settle();

    expect(toast.error).not.toHaveBeenCalled();
    // Still clickable — the button outlives the request it walked away from.
    await user.click(screen.getByRole("button", { name: "Read response aloud" }));
    await screen.findByRole("button", { name: "Preparing audio" });
  });
});

describe("the one refusal that survives chunking", () => {
  it("refuses a response with nothing speakable in it, and requests nothing", async () => {
    // LENGTH is never a refusal any more — the chunker hard-slices as a last
    // resort, so no chunk can exceed the cap and there is no length at which
    // the button gives up. What remains is a response that strips to nothing:
    // a horizontal rule, an empty bubble. Refusing UP FRONT matters because a
    // click that cannot produce audio must not silence whatever another turn
    // is reading aloud.
    const user = userEvent.setup();
    renderFooter("---\n\n***\n\n   ");

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("There is nothing here to read aloud."),
    );
    expect(synthesize).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Read response aloud" })).toBeInTheDocument();
  });

  it("reads a response far past the cap rather than refusing it for length", async () => {
    // The inverse, stated as its own case so the removal of the old
    // "refuses an over-long response" assertion is covered rather than lost.
    const huge = TEN_PARAGRAPHS.repeat(3);
    expect(huge.length).toBeGreaterThan(SERVER_CAP * 8);
    const user = userEvent.setup();
    renderFooter(huge);

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    await waitFor(() => expect(synthesize).toHaveBeenCalled());
    expect(toast.error).not.toHaveBeenCalled();
    expect(gates[0].text.length).toBeLessThanOrEqual(SERVER_CAP);
  });
});

describe("a chunk that fails mid-read", () => {
  it("reports it and stops, rather than falling silent as if the read finished", async () => {
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(gates).toHaveLength(2));

    endCurrentClip();
    gates[1].reject(
      new Error(
        JSON.stringify({
          error: { code: "speech_gateway_error", reason: "the gateway refused the call", retryable: true },
        }),
      ),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not read this aloud: the gateway refused the call",
      ),
    );
    await screen.findByRole("button", { name: "Read response aloud" });
    // Stopped cleanly: no chunk 3 was fetched to paper over the gap.
    await settle();
    expect(synthesize).toHaveBeenCalledTimes(2);
  });

  it("names a busy server as a wait rather than a fault", async () => {
    const user = userEvent.setup();
    renderFooter(TEN_PARAGRAPHS);
    await startReading(user);
    await waitFor(() => expect(gates).toHaveLength(2));

    endCurrentClip();
    gates[1].reject(
      new Error(
        JSON.stringify({
          error: { code: "speech_capacity_exhausted", reason: "all 4 speech slots are in use", retryable: true },
        }),
      ),
    );

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Speech is busy right now. Try again in a few seconds."),
    );
  });
});
