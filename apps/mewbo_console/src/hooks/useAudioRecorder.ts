import { useCallback, useEffect, useRef, useState } from "react";

import { TRANSCRIBE_TIMEOUT_MS, transcribeSpeech } from "../api/speech";
import { reasonFrom } from "../api/httpBase";

/**
 * The dictation state machine, headless — every composer's mic runs THIS, and
 * `MicButton` is the only thing that draws it. One copy of the machine is the
 * whole point: the states below are subtle (a cancel that must not reach the
 * network, a stream that must be released on three different exits) and a
 * second implementation would drift on exactly those points rather than on the
 * obvious ones.
 *
 *   idle ──start──▶ recording ⇄ paused ──stop──▶ transcribing ──▶ idle
 *                      └──────cancel──────┴─────────────────────▶ idle
 *                                          transcribing ──✗──▶ failed ──retry──▶ transcribing
 *
 * Three properties are load-bearing rather than incidental:
 *
 * 1. **Cancel sends NOTHING.** It stops the recorder like `stop` does, but sets
 *    a discard flag the stop handler reads first, so the blob is dropped before
 *    any request is built. The reason is cost: a discarded recording that still
 *    billed a transcription is the exact outcome cancel exists to prevent.
 * 2. **`failed` KEEPS the audio.** A failed transcription leaves the recording
 *    in hand so `retry` re-sends the same bytes. A dictated paragraph is
 *    expensive to produce and impossible to recover once dropped, so a
 *    transport failure must never be the thing that destroys it — the gateway
 *    marks its own errors `retryable`, and this is what lets a retry mean
 *    something.
 * 3. **Every exit releases the media stream.** Stop, cancel, a permission-less
 *    start and unmount all end at `releaseStream()`. A live track left running
 *    keeps the browser's recording indicator lit after the UI says it stopped,
 *    which is a privacy defect and reads as the app still listening.
 *
 * Cost class: `O(1)` per transition; `O(recording length)` for the one upload.
 */

/**
 * Containers to ask `MediaRecorder` for, best first.
 *
 * Feature-detected rather than hardcoded because there is no universal default:
 * Chrome and Firefox produce WebM/Opus, Safari produces MP4/AAC.
 *
 * **WebM/Opus is accepted by the gateway as-is — measured, not assumed** (a real
 * upload returned 200 in ~0.5s), which is what rules out both a client-side
 * conversion and a server-side transcode. Send whatever the browser encoded.
 * `audio/ogg` is deliberately ABSENT: Firefox would happily record it and the
 * endpoint does not list it, and Firefox supports `audio/webm` anyway.
 */
const MIME_CANDIDATES = [
  "audio/webm;codecs=opus",
  "audio/webm",
  "audio/mp4",
  "audio/wav",
] as const;

/**
 * Container → file extension. The server reads the EXTENSION as its format
 * hint and ignores the part's `Content-Type`, so this map is what actually
 * tells the gateway what it is decoding.
 */
const EXTENSION_BY_TYPE: Record<string, string> = {
  "audio/webm": "webm",
  "audio/mp4": "mp4",
  "audio/wav": "wav",
  "audio/x-wav": "wav",
  "audio/wave": "wav",
  "audio/mpeg": "mp3",
  "audio/ogg": "ogg",
};

/** Whether this browser can record at all — no `MediaRecorder`, no feature. */
export function recordingSupported(): boolean {
  return (
    typeof MediaRecorder !== "undefined" &&
    typeof navigator !== "undefined" &&
    typeof navigator.mediaDevices?.getUserMedia === "function"
  );
}

/** Whether this browser can pause mid-recording (Safari below 14.1 cannot). */
function pauseSupported(): boolean {
  return (
    typeof MediaRecorder !== "undefined" &&
    typeof MediaRecorder.prototype?.pause === "function"
  );
}

/** The best container this browser offers, or `undefined` to take its default. */
function pickMimeType(): string | undefined {
  if (typeof MediaRecorder?.isTypeSupported !== "function") return undefined;
  return MIME_CANDIDATES.find((candidate) => MediaRecorder.isTypeSupported(candidate));
}

/**
 * Upload filename for a recorded container, extension included.
 *
 * Exported because the extension is a wire contract rather than a detail — the
 * server reads it, so it is worth asserting on directly.
 */
export function fileNameFor(mimeType: string): string {
  const base = mimeType.split(";")[0]?.trim().toLowerCase() ?? "";
  return `recording.${EXTENSION_BY_TYPE[base] ?? "webm"}`;
}

/** Turn a `getUserMedia` rejection into something a person can act on. */
function captureFailureMessage(error: unknown): string {
  const name = error instanceof Error ? error.name : "";
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access is blocked. Allow it for this site in your browser settings, then try again.";
  }
  if (name === "NotFoundError" || name === "OverconstrainedError") {
    return "No microphone was found.";
  }
  if (name === "NotReadableError") {
    return "The microphone is already in use by another application.";
  }
  return reasonFrom(error) || "Could not start recording.";
}

export type RecorderStatus = "idle" | "recording" | "paused" | "transcribing" | "failed";

export interface UseAudioRecorderOptions {
  /** Receives the finished transcript. Never called with an empty string. */
  onTranscript: (text: string) => void;
  /** Announces a failure to the user; the hook itself renders nothing. */
  onError?: (message: string) => void;
  /** Server-published upload ceiling in bytes; `null` disables the local guard. */
  maxAudioBytes?: number | null;
}

export interface AudioRecorder {
  status: RecorderStatus;
  /** Captured audio so far, excluding paused time. */
  elapsedMs: number;
  /** Why the last transcription failed; non-null exactly while `failed`. */
  error: string | null;
  /** This browser can record. When false, render no control at all. */
  supported: boolean;
  /** This browser can pause mid-recording. */
  canPause: boolean;
  start: () => void;
  pause: () => void;
  resume: () => void;
  /** Discard everything and send NOTHING. Available while recording or paused. */
  cancel: () => void;
  /** End the recording and upload it for transcription. */
  stop: () => void;
  /** Re-send the retained recording after a failure. */
  retry: () => void;
  /** Drop the retained recording after a failure, without sending it. */
  discard: () => void;
}

export function useAudioRecorder({
  onTranscript,
  onError,
  maxAudioBytes = null,
}: UseAudioRecorderOptions): AudioRecorder {
  const [status, setStatus] = useState<RecorderStatus>("idle");
  const [elapsedMs, setElapsedMs] = useState(0);
  const [error, setError] = useState<string | null>(null);

  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const blobRef = useRef<Blob | null>(null);
  const mimeTypeRef = useRef<string>("audio/webm");
  // Read by the recorder's own stop handler. `stop` and `cancel` both end the
  // recorder the same way; this flag is the ONLY thing that distinguishes an
  // upload from a discard, which is why it is set before `.stop()` in both.
  const discardRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const startedAtRef = useRef(0);
  const accruedRef = useRef(0);
  const mountedRef = useRef(true);

  // Callbacks live behind refs so the transition functions can stay stable
  // across renders — a composer re-renders on every keystroke, and a mic whose
  // handlers changed identity that often would re-arm its timers with it.
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;
  const maxBytesRef = useRef(maxAudioBytes);
  maxBytesRef.current = maxAudioBytes;

  const supported = recordingSupported();
  const canPause = pauseSupported();

  const stopTick = useCallback(() => {
    if (tickRef.current !== null) clearInterval(tickRef.current);
    tickRef.current = null;
  }, []);

  const startTick = useCallback(() => {
    stopTick();
    // Elapsed is recomputed from timestamps rather than incremented, so a
    // throttled background tab resumes with the true duration instead of a
    // count of ticks that did fire.
    tickRef.current = setInterval(() => {
      setElapsedMs(accruedRef.current + (Date.now() - startedAtRef.current));
    }, 500);
  }, [stopTick]);

  const releaseStream = useCallback(() => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    recorderRef.current = null;
  }, []);

  const clearRequest = useCallback(() => {
    if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
    timeoutRef.current = null;
    abortRef.current = null;
  }, []);

  const upload = useCallback(
    async (blob: Blob) => {
      const ceiling = maxBytesRef.current;
      if (ceiling !== null && blob.size > ceiling) {
        // Refused locally rather than uploaded into a 413: the request would
        // have to carry every byte before the server could reject it.
        blobRef.current = null;
        const limit = `${Math.floor(ceiling / (1024 * 1024))} MB`;
        const message = `That recording is too long to transcribe (over ${limit}). Record a shorter one.`;
        setError(null);
        setStatus("idle");
        onErrorRef.current?.(message);
        return;
      }

      const controller = new AbortController();
      abortRef.current = controller;
      let timedOut = false;
      timeoutRef.current = setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, TRANSCRIBE_TIMEOUT_MS);
      setStatus("transcribing");
      try {
        const text = await transcribeSpeech(blob, fileNameFor(mimeTypeRef.current), controller.signal);
        if (!mountedRef.current) return;
        blobRef.current = null;
        setStatus("idle");
        setElapsedMs(0);
        const trimmed = text.trim();
        if (trimmed) onTranscriptRef.current(trimmed);
      } catch (err) {
        if (!mountedRef.current) return;
        // An abort that was NOT our timeout is an unmount racing the response;
        // there is nobody left to tell, and the recording is going away anyway.
        if (controller.signal.aborted && !timedOut) return;
        const message = timedOut
          ? "Transcription took too long and was stopped. The recording is still here — try again."
          : reasonFrom(err);
        // The blob stays in hand, which is what makes `retry` re-send the same
        // audio instead of asking the user to say it all again.
        setError(message);
        setStatus("failed");
        onErrorRef.current?.(message);
      } finally {
        clearRequest();
      }
    },
    [clearRequest],
  );

  const start = useCallback(async () => {
    if (!recordingSupported()) return;
    setError(null);
    blobRef.current = null;
    chunksRef.current = [];
    discardRef.current = false;
    accruedRef.current = 0;
    setElapsedMs(0);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      if (!mountedRef.current) {
        // Permission resolved after the composer went away — the tracks are
        // live and nothing will ever stop them unless we do it here.
        stream.getTracks().forEach((track) => track.stop());
        return;
      }
      streamRef.current = stream;
      const preferred = pickMimeType();
      const recorder = new MediaRecorder(stream, preferred ? { mimeType: preferred } : undefined);
      // The recorder's OWN `mimeType` wins: asking for a container is not the
      // same as getting one, and the extension has to describe what was
      // actually encoded.
      mimeTypeRef.current = recorder.mimeType || preferred || "audio/webm";
      recorder.ondataavailable = (event: BlobEvent) => {
        if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        const chunks = chunksRef.current;
        chunksRef.current = [];
        releaseStream();
        if (discardRef.current) {
          // Cancel. The bytes are dropped here, before a request exists.
          discardRef.current = false;
          return;
        }
        const blob = new Blob(chunks, { type: mimeTypeRef.current });
        blobRef.current = blob;
        void upload(blob);
      };
      recorderRef.current = recorder;
      recorder.start();
      startedAtRef.current = Date.now();
      startTick();
      setStatus("recording");
    } catch (err) {
      releaseStream();
      if (!mountedRef.current) return;
      // No recording was ever captured, so there is nothing to retain and
      // nothing to retry — the control returns to idle and the message is
      // announced. A wedged error state here would be a dead end.
      setStatus("idle");
      onErrorRef.current?.(captureFailureMessage(err));
    }
  }, [releaseStream, startTick, upload]);

  const pause = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state !== "recording") return;
    recorder.pause();
    accruedRef.current += Date.now() - startedAtRef.current;
    setElapsedMs(accruedRef.current);
    stopTick();
    setStatus("paused");
  }, [stopTick]);

  const resume = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state !== "paused") return;
    recorder.resume();
    startedAtRef.current = Date.now();
    startTick();
    setStatus("recording");
  }, [startTick]);

  const cancel = useCallback(() => {
    discardRef.current = true;
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.stop();
    } else {
      discardRef.current = false;
      releaseStream();
    }
    chunksRef.current = [];
    blobRef.current = null;
    stopTick();
    accruedRef.current = 0;
    setElapsedMs(0);
    setError(null);
    setStatus("idle");
  }, [releaseStream, stopTick]);

  const stop = useCallback(() => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return;
    discardRef.current = false;
    stopTick();
    // Set before the recorder's stop handler runs so the spinner appears on
    // the click rather than a beat later, when the blob happens to assemble.
    setStatus("transcribing");
    recorder.stop();
  }, [stopTick]);

  const retry = useCallback(() => {
    const blob = blobRef.current;
    if (!blob) return;
    setError(null);
    void upload(blob);
  }, [upload]);

  const discard = useCallback(() => {
    blobRef.current = null;
    setError(null);
    setElapsedMs(0);
    setStatus("idle");
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      stopTick();
      if (timeoutRef.current !== null) clearTimeout(timeoutRef.current);
      abortRef.current?.abort();
      // A recorder still running would fire its stop handler and upload a
      // recording nobody is waiting for; the flag makes that path discard.
      discardRef.current = true;
      const recorder = recorderRef.current;
      if (recorder && recorder.state !== "inactive") {
        try {
          recorder.stop();
        } catch {
          /* already torn down by the browser — nothing to unwind */
        }
      }
      releaseStream();
    };
  }, [releaseStream, stopTick]);

  return {
    status,
    elapsedMs,
    error,
    supported,
    canPause,
    start,
    pause,
    resume,
    cancel,
    stop,
    retry,
    discard,
  };
}
