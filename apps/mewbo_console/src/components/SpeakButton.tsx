import { useCallback, useEffect, useRef, useState } from 'react';
import { Loader2, Square, Volume2 } from 'lucide-react';
import { toast } from 'sonner';
import { synthesizeSpeech } from '../api/speech';
import { codeFrom, reasonFrom } from '../api/httpBase';
import { useSpeechCapability } from '../hooks/useSpeechCapability';
import { chunkForSpeech } from '../utils/speechChunks';
import { speechReader } from '../utils/speechPlayback';
import { Button } from './ui/button';

type SpeakState = 'idle' | 'loading' | 'playing';

/**
 * Every in-flight synthesis slot on the server is taken. Retryable, ordinary
 * under load, and NOT a fault of this request — so it gets its own sentence
 * rather than being dressed up as a failure the user should investigate.
 */
const CAPACITY_CODE = 'speech_capacity_exhausted';

/**
 * Read-aloud button with state-swap feedback — `CopyButton`'s sibling in the
 * turn footer's right cluster, built on the same ghost/iconOnly/sm `Button`
 * and taking the same `className` passthrough for the caller's sizing.
 *
 * Three states, because the wait is long enough to see: idle → loading while
 * the FIRST chunk is in flight → playing, which is also the stop control.
 * Synthesis costs roughly half a second for a sentence and four seconds for a
 * paragraph (measured against the deployed gateway), so the loading state is
 * load-bearing rather than polish — without it a click looks like it did
 * nothing. Loading covers the first chunk only; every later chunk is
 * synthesized during playback and never returns the button to a wait.
 *
 * The button is absent, not disabled, when the server advertises no speech: a
 * disabled control promises a capability that is not coming back.
 *
 * **The response is CHUNKED, and that is a correctness fix before it is a
 * latency one.** Synthesis is buffered end to end (the gateway sends nothing
 * until the whole file exists, so `stream=true` is a measured no-op) and the
 * endpoint refuses input over `max_text_chars` — so a single request for a long
 * answer both failed outright and, when it did succeed, played nothing until
 * the whole thing had been generated. `chunkForSpeech` splits on natural
 * boundaries and `speechReader` plays chunk N while chunk N+1 synthesizes.
 *
 * **Stop means stop, for the listener.** `speechReader.stop()` silences audio
 * synchronously and issues no further requests, so abandoning a ten-paragraph
 * read costs the chunk already in flight rather than all ten. It deliberately
 * does NOT abort that one — cancelling leaves the backend synthesizing anyway
 * while holding a slot it shares with every other caller, which is measured in
 * `speechPlayback.ts` and is far more expensive than the audio it discards.
 */
export function SpeakButton({ text, className = '', label = 'Read aloud' }: {
  text: string;
  className?: string;
  /** Accessible name for the idle state; the other states name themselves. */
  label?: string;
}) {
  const { canSynthesize, maxTextChars } = useSpeechCapability();
  const [state, setState] = useState<SpeakState>('idle');
  // Bumped by every stop and every new read, so a chunk that resolves after it
  // stopped mattering cannot drive the UI. Comparing a captured token is what
  // makes "clicked stop during a 4s synthesis" resolve to idle instead of
  // lurching into playback a moment later — and, since the reader is shared
  // console-wide, what keeps turn 3's callbacks off its own button once turn
  // 12 has pre-empted it.
  const tokenRef = useRef(0);
  // True exactly while this instance owns the reader — read only by the unmount
  // cleanup, so navigating away mid-sentence does not leave audio playing with
  // nothing left on screen to stop it.
  const activeRef = useRef(false);

  const stop = useCallback(() => {
    tokenRef.current += 1;
    activeRef.current = false;
    speechReader.stop();
    setState('idle');
  }, []);

  useEffect(() => () => {
    if (!activeRef.current) return;
    speechReader.stop();
  }, []);

  const speak = useCallback(async () => {
    // Chunked BEFORE anything is stopped, for the same reason the old
    // over-long refusal was: a click that cannot produce audio must not silence
    // whatever another turn is reading aloud. The ceiling comes off the
    // capability document, never a client copy — a copy of a server policy
    // number goes stale in silence.
    const chunks = chunkForSpeech(text, { limitChars: maxTextChars });
    if (chunks.length === 0) {
      toast.error('There is nothing here to read aloud.');
      return;
    }
    stop();
    const token = tokenRef.current;
    activeRef.current = true;
    setState('loading');
    await speechReader.start({
      chunks,
      synthesize: synthesizeSpeech,
      onEnd: () => {
        if (token !== tokenRef.current) return;
        activeRef.current = false;
        setState('idle');
      },
      onError: (error) => {
        // A superseded read is the user's own doing, not a failure to report,
        // and the token is the whole guard: the reader never reports an error
        // for a read it already stopped, and nothing here cancels a request, so
        // there is no AbortError to filter out either. Anything reaching this
        // line is a chunk that genuinely failed, and swallowing it would let a
        // read that died three paragraphs in look exactly like one that
        // finished — the only symptom is that the audio simply stops.
        if (token !== tokenRef.current) return;
        // `reasonFrom`, never the raw message: the namespace answers every
        // refusal in the `{error: {code, reason, retryable}}` envelope, so the
        // unparsed `Error.message` is a JSON blob. `codeFrom` is the branch — a
        // busy server is a wait, not a fault, and reads as one.
        toast.error(
          codeFrom(error) === CAPACITY_CODE
            ? 'Speech is busy right now. Try again in a few seconds.'
            : `Could not read this aloud: ${reasonFrom(error)}`,
        );
      },
    });
    // `start` resolves once the first chunk is PLAYING — or once the read has
    // already ended, in which case `onEnd` has moved the button on and this
    // must not drag it back. `activeRef` is the flag it cleared to say so.
    if (token !== tokenRef.current || !activeRef.current) return;
    setState('playing');
  }, [maxTextChars, stop, text]);

  if (!canSynthesize) return null;

  const { icon, name } = {
    idle: { icon: <Volume2 className="w-3 h-3" />, name: label },
    loading: { icon: <Loader2 className="w-3 h-3 animate-spin" />, name: 'Preparing audio' },
    playing: { icon: <Square className="w-3 h-3 fill-current" />, name: 'Stop reading' },
  }[state];

  return (
    <Button
      variant="ghost"
      size="sm"
      iconOnly
      leadingIcon={icon}
      onClick={(e) => {
        e.stopPropagation();
        if (state === 'idle') void speak();
        else stop();
      }}
      aria-label={name}
      title={name}
      className={className}
    />
  );
}
