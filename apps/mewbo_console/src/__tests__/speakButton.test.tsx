/**
 * The read-aloud button, driven through the REAL `AssistantTurnFooter`.
 *
 * Rendering the actual footer rather than the button alone is the point of the
 * file: recon found a second, fully-formed action row in
 * `assistant-ui/thread.tsx` (`AssistantActionBar` / `ActionBarPrimitive.Copy`)
 * that nothing in `src` imports. A speaker button built there would pass every
 * unit test and never appear on screen. Asserting it lands beside the Copy
 * button inside the footer that the timeline actually mounts is what rules that
 * out.
 *
 * The properties pinned here are the ones a user feels:
 *
 * 1. It sits in the same cluster as Copy, and Copy still works — the row is not
 *    reshaped by adding to it.
 * 2. A click shows a loading state IMMEDIATELY. Synthesis is buffered whole and
 *    costs seconds for a paragraph (measured), so a click with no feedback
 *    reads as a dead button.
 * 3. Playing is also the stop control, and stopping returns to idle.
 * 4. No speech advertised means no button at all, not a disabled one.
 * 5. A failed synthesis returns to idle. A button stuck spinning forever is the
 *    failure mode that outlives the request that caused it.
 *
 * Only the HTTP module and the toast sink are stubbed; the button, the footer,
 * the capability hook, the query cache and the playback singleton are all real.
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
import { speechReader } from "../utils/speechPlayback";
import type { TurnMeta } from "../types";

const TURN: TurnMeta = {
  id: "turn-1",
  events: [{ ts: "2026-01-02T03:04:05Z", type: "completion", payload: {} }],
  files: [],
  model: "claude-sonnet-5",
};

function renderFooter(responseText = "A sentence worth hearing.") {
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

/** A promise whose resolution this test controls, standing in for a slow call. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

const capability = vi.mocked(speechApi.fetchSpeechCapability);
const synthesize = vi.mocked(speechApi.synthesizeSpeech);

beforeEach(() => {
  vi.clearAllMocks();
  capability.mockResolvedValue({ synthesis: true, transcription: false, maxAudioBytes: null, maxTextChars: 2000 });
  synthesize.mockResolvedValue(new Blob(["audio"]));
});

afterEach(() => {
  // The SEQUENCER, not the player: a read now outlives one clip, so stopping
  // only the player would leave a half-finished read able to synthesize into
  // the next test.
  speechReader.stop();
  cleanup();
});

describe("read-aloud button in the assistant turn footer", () => {
  it("renders beside the Copy button in the footer's interactive cluster", async () => {
    renderFooter();

    const speak = await screen.findByRole("button", { name: "Read response aloud" });
    const copy = screen.getByRole("button", { name: "Copy response" });
    // Same parent element, and Copy comes first — the cluster is
    // copy · read aloud · Trace · overflow, in that order.
    expect(speak.parentElement).toBe(copy.parentElement);
    expect(copy.compareDocumentPosition(speak) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The rest of the row is untouched by the addition.
    expect(screen.getByRole("button", { name: "Trace" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More turn actions" })).toBeInTheDocument();
  });

  it("synthesizes the response text on click and shows a loading state at once", async () => {
    const pending = deferred<Blob>();
    synthesize.mockReturnValue(pending.promise);
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    // Loading is visible while the request is still outstanding — asserted
    // BEFORE resolving, which is the only ordering that proves it is not a
    // frame that appears once the audio already arrived.
    const loading = await screen.findByRole("button", { name: "Preparing audio" });
    expect(loading).toBeInTheDocument();
    expect(synthesize).toHaveBeenCalledTimes(1);
    expect(synthesize.mock.calls[0][0]).toBe("A sentence worth hearing.");

    pending.resolve(new Blob(["audio"]));
    await screen.findByRole("button", { name: "Stop reading" });
  });

  it("toggles back to idle when the playing button is clicked again", async () => {
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));
    const playing = await screen.findByRole("button", { name: "Stop reading" });

    await user.click(playing);

    await screen.findByRole("button", { name: "Read response aloud" });
    expect(screen.queryByRole("button", { name: "Stop reading" })).toBeNull();
  });

  it("returns to idle on its own when playback finishes", async () => {
    // The player's `<audio>` is never attached to the document, so the only way
    // to reach it is to capture it as it is constructed. Firing the real
    // `ended` event on it is what distinguishes this from the click-to-stop
    // case: nothing touches the button, so it exercises the exit that strands a
    // stop control if `onEnd` is ever wired to only some of the player's paths.
    const created: HTMLAudioElement[] = [];
    const RealAudio = window.Audio;
    vi.stubGlobal(
      "Audio",
      class CapturedAudio extends RealAudio {
        constructor(src?: string) {
          super(src);
          created.push(this);
        }
      },
    );
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));
    await screen.findByRole("button", { name: "Stop reading" });
    expect(created).toHaveLength(1);

    created[0].dispatchEvent(new Event("ended"));

    await screen.findByRole("button", { name: "Read response aloud" });
    vi.unstubAllGlobals();
  });

  it("renders nothing when the server advertises no speech", async () => {
    capability.mockResolvedValue({ synthesis: false, transcription: false, maxAudioBytes: null, maxTextChars: 2000 });
    renderFooter();

    // Copy proves the footer itself rendered, so the speaker's absence is a
    // decision rather than a failed render.
    await screen.findByRole("button", { name: "Copy response" });
    await waitFor(() => expect(capability).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Read response aloud" })).toBeNull();
    expect(synthesize).not.toHaveBeenCalled();
  });

  it("renders nothing when the capability probe fails outright", async () => {
    // A server with no speech routes at all answers 404, and fail-closed means
    // that hides the control rather than showing one that cannot work.
    capability.mockRejectedValue(new Error("Request failed: 404"));
    renderFooter();

    await screen.findByRole("button", { name: "Copy response" });
    await waitFor(() => expect(capability).toHaveBeenCalled());
    expect(screen.queryByRole("button", { name: "Read response aloud" })).toBeNull();
  });

  // An over-long response is CHUNKED, not refused — the length ceiling stopped
  // being a wall once `chunkForSpeech` landed. The refusal test that used to
  // sit here is superseded by `speakButtonChunked.test.tsx`'s "splits it into
  // several requests rather than refusing it for length", which also pins the
  // ceiling as server-published rather than a client constant.

  it("reads the refusal envelope's reason rather than the raw JSON message", async () => {
    // Every refusal in this namespace arrives as `{error:{code,reason,retryable}}`,
    // and `readError` stringifies that whole object into `Error.message`. Showing
    // the message verbatim would put a JSON blob in front of the user.
    synthesize.mockRejectedValue(
      new Error(
        JSON.stringify({
          error: { code: "speech_gateway_error", reason: "the gateway refused the call", retryable: true },
        }),
      ),
    );
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Could not read this aloud: the gateway refused the call",
      ),
    );
    expect(toast.error).not.toHaveBeenCalledWith(expect.stringContaining("retryable"));
  });

  it("tells the user to wait when every synthesis slot is taken", async () => {
    // A bounded-concurrency refusal is a wait, not a fault. Rendering it as a
    // generic failure would send someone hunting a problem that is not theirs.
    synthesize.mockRejectedValue(
      new Error(
        JSON.stringify({
          error: {
            code: "speech_capacity_exhausted",
            reason: "all 4 speech slots are in use",
            retryable: true,
          },
        }),
      ),
    );
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Speech is busy right now. Try again in a few seconds.",
      ),
    );
    await screen.findByRole("button", { name: "Read response aloud" });
  });

  it("recovers to idle and reports when synthesis fails, never wedging in loading", async () => {
    synthesize.mockRejectedValue(new Error("gateway said no"));
    const user = userEvent.setup();
    renderFooter();

    await user.click(await screen.findByRole("button", { name: "Read response aloud" }));

    // Back to idle, and clickable again — the button outlives its own failure.
    await screen.findByRole("button", { name: "Read response aloud" });
    expect(screen.queryByRole("button", { name: "Preparing audio" })).toBeNull();
    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Could not read this aloud: gateway said no"),
    );

    synthesize.mockResolvedValue(new Blob(["audio"]));
    await user.click(screen.getByRole("button", { name: "Read response aloud" }));
    await screen.findByRole("button", { name: "Stop reading" });
  });
});
