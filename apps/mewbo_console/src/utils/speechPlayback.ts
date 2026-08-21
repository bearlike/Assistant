/**
 * The console plays ONE read at a time, console-wide.
 *
 * Every assistant turn carries its own speaker button, so exclusivity cannot
 * live in a component: pressing turn 12's button has to silence turn 3's, and
 * turn 3 has to fall back to idle without knowing turn 12 exists. This module
 * is the single owner of "what is playing right now" — module-scope instances
 * that any button can pre-empt.
 *
 * It also owns the object-URL lifetime. A synthesized paragraph is a couple of
 * megabytes; an unrevoked blob URL pins that for the life of the document, so
 * the revoke is paired with the pause in one place rather than trusted to each
 * caller's cleanup path.
 *
 * Two layers, because a read is no longer one clip:
 *
 * - `SpeechPlayer` (`speechPlayer`) plays ONE clip and owns its URL.
 * - `SpeechReader` (`speechReader`) sequences a whole response through it,
 *   synthesizing the next chunk while the current one plays.
 *
 * ⚠️ **`speechReader` is the entry point; a surface reading text aloud must not
 * drive `speechPlayer` directly.** Pre-emption has to reach the SEQUENCER, not
 * just the clip: silencing the player alone would look like the current chunk
 * ending naturally, and the abandoned read would answer by synthesizing and
 * playing its next chunk over the top of whatever pre-empted it.
 */

class SpeechPlayer {
  private audio: HTMLAudioElement | null = null;
  private objectUrl: string | null = null;
  private onEnd: (() => void) | null = null;
  /**
   * The clip after this one, already constructed and buffering.
   *
   * A chunked read knows its next clip seconds before it needs it, and building
   * the element at the moment the current one ends puts URL creation plus the
   * first decode inside the gap between chunks. Preparing it early moves both
   * off that seam.
   */
  private next: { blob: Blob; audio: HTMLAudioElement; url: string } | null = null;

  /**
   * Prepare `blob` for playback without starting it.
   *
   * Advisory in both directions: a later `play(blob)` with the SAME blob adopts
   * this element, any other blob discards it, and skipping the call entirely
   * only costs the gap it was meant to remove. So a caller may preload
   * optimistically and never has to reason about whether the preload was used.
   *
   * Cost class: `O(1)`.
   */
  preload(blob: Blob): void {
    this.discardNext();
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    audio.preload = "auto";
    audio.load();
    this.next = { blob, audio, url };
  }

  /** Take the preloaded clip if it is for `blob`; release it otherwise. */
  private takeNext(blob: Blob): { audio: HTMLAudioElement; url: string } | null {
    const held = this.next;
    this.next = null;
    if (!held) return null;
    if (held.blob === blob) return { audio: held.audio, url: held.url };
    URL.revokeObjectURL(held.url);
    return null;
  }

  private discardNext(): void {
    const held = this.next;
    this.next = null;
    if (held) URL.revokeObjectURL(held.url);
  }

  /**
   * Play `blob`, silencing whatever was playing first.
   *
   * `onEnd` fires on EVERY exit — finished, failed, stopped, or pre-empted by
   * another turn's button — so a caller has exactly one path back to idle and
   * cannot be stranded showing a stop control for audio that ended.
   *
   * The container is never declared: the blob URL is handed to the element
   * untyped and the browser sniffs the bytes, which is the only thing that
   * works when the gateway's own `Content-Type` is wrong on every response.
   *
   * Rejects if the browser refuses playback (an autoplay policy, a decode
   * failure); `onEnd` has already fired by then.
   *
   * Cost class: `O(1)`.
   */
  async play(blob: Blob, onEnd: () => void): Promise<void> {
    // Claimed BEFORE `stop()`, which releases whatever preload is still held:
    // the clip this call is about to play must survive its own predecessor's
    // teardown.
    const adopted = this.takeNext(blob);
    this.stop();
    const url = adopted?.url ?? URL.createObjectURL(blob);
    const audio = adopted?.audio ?? new Audio(url);
    this.audio = audio;
    this.objectUrl = url;
    this.onEnd = onEnd;
    // Guarded against a stale element: a settled `audio` that has since been
    // replaced must not tear down its successor.
    const settle = () => {
      if (this.audio === audio) this.endClip();
    };
    audio.onended = settle;
    audio.onerror = settle;
    try {
      await audio.play();
    } catch (error) {
      settle();
      throw error;
    }
  }

  /**
   * Silence whatever is playing, release its blob URL, and notify its owner.
   *
   * Idempotent, and safe to call from within an `onEnd` callback: the fields
   * are cleared BEFORE the notification, so a re-entrant call sees an already
   * empty player rather than double-revoking a URL.
   *
   * Cost class: `O(1)`.
   */
  stop(): void {
    this.discardNext();
    this.endClip();
  }

  /**
   * End the CURRENT clip, leaving any preload for whatever plays next.
   *
   * ⚠️ **The two lifetimes are different, and collapsing them defeats the
   * preload entirely.** A clip ending naturally runs through here, and its
   * `onEnd` is what tells the sequencer to play the clip that was preloaded
   * DURING it — so releasing the preload on a clip's own end would discard it a
   * moment before the caller asks for it, and every chunk boundary would pay
   * for a fresh element after all. Only `stop()`, which ends the whole read,
   * releases it.
   */
  private endClip(): void {
    const audio = this.audio;
    const url = this.objectUrl;
    const onEnd = this.onEnd;
    this.audio = null;
    this.objectUrl = null;
    this.onEnd = null;
    audio?.pause();
    if (url) URL.revokeObjectURL(url);
    onEnd?.();
  }
}

export const speechPlayer = new SpeechPlayer();

/**
 * Synthesize one chunk. Injected so the sequencer owns no transport of its own.
 *
 * **Deliberately takes NO `AbortSignal`**, though `synthesizeSpeech` offers one.
 * The narrower type is the enforcement: see `SpeechReader.stop()` for why
 * cancelling a synthesis costs more than letting it finish.
 */
export type SynthesizeChunk = (text: string) => Promise<Blob>;

export interface SpeechReadRequest {
  /** Ordered chunks from `utils/speechChunks.ts`. An empty list ends at once. */
  chunks: string[];
  synthesize: SynthesizeChunk;
  /**
   * Fires on EVERY exit — finished, stopped, pre-empted by another turn, or
   * failed — so a caller has exactly one path back to idle. On a failure it
   * fires BEFORE `onError`, which reports what went wrong rather than deciding
   * the UI state.
   */
  onEnd: () => void;
  /** A chunk could not be synthesized or played. The read has already ended. */
  onError: (error: unknown) => void;
}

/**
 * Read a whole response aloud, one chunk at a time, with one chunk of lookahead.
 *
 * **Exactly one synthesis runs ahead of playback, and that number is a budget,
 * not a heuristic.** Two limits sit behind it, at different layers: the API
 * refuses a fifth concurrent speech call with `speech_capacity_exhausted`, and
 * the TTS backend underneath it runs only **two** `max_parallel_requests` —
 * both shared across every caller, neither per user. So fanning a ten-chunk
 * response out at once would exhaust the pool on one click, 503 its own later
 * chunks, and stall speech for everybody else.
 *
 * ⚠️ **One ahead is a budget the CHUNKER is sized against, not slack.** An
 * earlier version of this docstring justified the depth by claiming synthesis
 * outruns playback "by more than six to one"; re-measured against the deployed
 * gateway the rate ratio is about **2.1 to 1** (`speechChunks.ts` carries the
 * fit), so the margin is real but nothing like ample. What closes the gap is
 * `coverageLimit()` there, which sizes each chunk against what the PREVIOUS
 * one's playback can pay for — not a deeper pipeline here.
 *
 * **A depth of two was measured and REJECTED.** Two 500-600 character chunks
 * issued concurrently against the live gateway took 28.1 s wall to the same
 * pair's 21.2 s issued one after the other — a 0.75x *slowdown*, because the
 * two-wide backend simply queues the second request while the client holds
 * both. Deeper lookahead does not buy earlier audio here; it buys contention,
 * and it pre-pays for audio a stop is about to discard.
 *
 * **Stopping stops spending.** Every exit runs through `stop()`, which drops the
 * chunks not yet requested and bumps a generation counter that strands every
 * callback already scheduled — so a read abandoned two seconds into a
 * ten-paragraph answer issues no further requests, rather than paying for the
 * other eight and throwing the audio away. What it does NOT do is abort the one
 * request already in flight; `stop()` explains why.
 *
 * Cost class: `O(1)` per chunk transition; `O(chunks)` requests over a full read.
 */
class SpeechReader {
  private chunks: string[] = [];
  private index = 0;
  private synthesize: SynthesizeChunk | null = null;
  private handlers: Pick<SpeechReadRequest, "onEnd" | "onError"> | null = null;
  /** The synthesis running ahead of playback, or `null` when none is. */
  private ahead: Promise<Blob> | null = null;
  /**
   * Bumped by every exit. Each async continuation captures the value it started
   * under and returns early if it no longer matches — which is what makes a
   * stop DURING a four-second synthesis resolve to silence instead of lurching
   * into playback once the bytes arrive.
   */
  private generation = 0;

  /** True while a read owns the player or has a request in flight. */
  get isReading(): boolean {
    return this.handlers !== null;
  }

  /**
   * Start reading `chunks`, pre-empting whatever was already being read.
   *
   * Resolves once the first chunk is playing (or the read has ended); the rest
   * of the read continues on its own. Never rejects — a failure is reported
   * through `onError` so a caller has one error path rather than two.
   */
  async start({ chunks, synthesize, onEnd, onError }: SpeechReadRequest): Promise<void> {
    this.stop();
    if (chunks.length === 0) {
      onEnd();
      return;
    }
    const generation = this.generation;
    this.chunks = chunks;
    this.index = 0;
    this.synthesize = synthesize;
    this.handlers = { onEnd, onError };

    let first: Blob;
    try {
      first = await synthesize(chunks[0]);
    } catch (error) {
      if (generation !== this.generation) return;
      this.fail(error);
      return;
    }
    if (generation !== this.generation) return;
    await this.playChunk(0, first, generation);
  }

  /**
   * End the current read: silence playback immediately, drop every chunk not
   * yet requested, and notify the owner.
   *
   * ⚠️ **This deliberately does NOT abort the request already in flight, and
   * that is the opposite of the obvious implementation. Do not "fix" it.**
   * Aborting the HTTP request does not stop the backend — the abandoned
   * synthesis keeps running and keeps holding one of the TTS backend's **two**
   * `max_parallel_requests`, which are shared across every caller of the
   * gateway rather than being per user. Measured against the live gateway,
   * three times:
   *
   * | after                          | next synthesis |
   * |--------------------------------|----------------|
   * | no cancellation (control)      | 1.09 s         |
   * | a cancellation                 | 14.89 s        |
   * | a cancellation, fresh client   | 13.90 s        |
   * | a cancellation, waited 6 s     | 8.53 s         |
   *
   * A fresh client being equally slow rules out a poisoned local connection
   * pool: the occupancy is server-side. The magnitude — 10+ s of recovery for
   * what was a ~4 s paragraph — is UNEXPLAINED and is written down as
   * unexplained rather than guessed at; the design only relies on the ordering,
   * which reproduced every time.
   *
   * So the compute is spent either way, and aborting merely adds a 13.7x
   * penalty to whoever calls next — including this user's own next click. The
   * cheapest stop is to let the current chunk finish and throw its audio away,
   * which the generation counter already does. Two quick stop-and-restart
   * cycles on an aborting implementation could stall TTS for every user of the
   * gateway.
   *
   * **None of this delays the user.** Playback is silenced synchronously here;
   * only the invisible background request is allowed to drain.
   *
   * Idempotent, and safe to call from within `onEnd` — the fields are cleared
   * BEFORE the notification, so a re-entrant call sees an already-idle reader.
   *
   * Cost class: `O(1)`.
   */
  stop(): void {
    const handlers = this.handlers;
    // Four guards, deliberately redundant: any ONE of them stops the pipeline,
    // so no single-line mutation can break the "no further requests" law. The
    // regression they exist for is a rewrite that silences the PLAYER and
    // forgets the sequencer, which trips all three cancellation tests at once.
    this.generation += 1;
    this.handlers = null;
    this.chunks = [];
    this.index = 0;
    this.synthesize = null;
    // Dropped, not cancelled: whatever it resolves to fails the generation
    // check and is discarded, and no chunk after it is ever requested.
    this.ahead = null;
    speechPlayer.stop();
    handlers?.onEnd();
  }

  /** Play chunk `index`, having first put chunk `index + 1` in flight. */
  private async playChunk(index: number, blob: Blob, generation: number): Promise<void> {
    this.index = index;
    this.prefetch(index + 1, generation);
    try {
      await speechPlayer.play(blob, () => {
        // Deferred, because `play()` fires this on its way OUT of a rejection
        // too. Running the advance a microtask later lets the `catch` below end
        // the read first, so a clip that failed to play cannot also be treated
        // as a clip that finished.
        queueMicrotask(() => void this.advance(generation));
      });
    } catch (error) {
      if (generation !== this.generation) return;
      this.fail(error);
    }
  }

  /** Move to the next chunk, or end the read when there is none. */
  private async advance(generation: number): Promise<void> {
    if (generation !== this.generation) return;
    const next = this.index + 1;
    if (next >= this.chunks.length) {
      this.stop();
      return;
    }
    const pending = this.ahead;
    this.ahead = null;
    let blob: Blob;
    try {
      // `pending` is the normal path; the fallback covers a prefetch that never
      // started (the very first advance after a preload was skipped).
      blob = pending ? await pending : await this.request(next);
    } catch (error) {
      if (generation !== this.generation) return;
      this.fail(error);
      return;
    }
    if (generation !== this.generation) return;
    await this.playChunk(next, blob, generation);
  }

  /** Put chunk `index` in flight, and hand its blob to the player early. */
  private prefetch(index: number, generation: number): void {
    if (index >= this.chunks.length) {
      this.ahead = null;
      return;
    }
    const pending = this.request(index);
    // Two handlers, one promise, and both are needed. `advance` is what READS
    // the rejection; this pair only keeps an abort — the expected outcome of a
    // stop — from surfacing as an unhandled rejection when nothing ever awaits.
    pending
      .then((blob) => {
        if (generation === this.generation) speechPlayer.preload(blob);
      })
      .catch(() => undefined);
    this.ahead = pending;
  }

  private request(index: number): Promise<Blob> {
    const synthesize = this.synthesize;
    if (!synthesize) return Promise.reject(new Error("speech read is not active"));
    return synthesize(this.chunks[index]);
  }

  /** End the read, then report why. Order matters: idle first, message second. */
  private fail(error: unknown): void {
    const onError = this.handlers?.onError;
    this.stop();
    onError?.(error);
  }
}

export const speechReader = new SpeechReader();
