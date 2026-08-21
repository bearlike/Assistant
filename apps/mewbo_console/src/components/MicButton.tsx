import { Loader2, Mic, Pause, Play, RotateCcw, Square, X } from 'lucide-react';
import { toast } from 'sonner';

import { useAudioRecorder } from '../hooks/useAudioRecorder';
import { useSpeechCapability } from '../hooks/useSpeechCapability';
import { cn } from '../lib/utils';
import { Button } from './ui/button';

/**
 * Dictation control — the ONE mic in the console, worn by every composer.
 *
 * `CopyButton`'s sibling in shape: the shared ghost/iconOnly/sm `Button`, both
 * `aria-label` and `title`, a `className` passthrough for the caller's sizing.
 * All state lives in `useAudioRecorder`; this file only decides what a state
 * looks like, which is what lets four composers share one machine.
 *
 * While recording it grows from one button into a small cluster —
 * `[discard] [pause] [elapsed] [stop]` — because record, pause and cancel are
 * three different intentions and collapsing them into one morphing button makes
 * every click a guess. It shrinks back the moment recording ends.
 *
 * **State is never carried by colour alone.** The icon changes with every
 * transition, the accessible name changes with it, the elapsed clock only runs
 * while audio is actually being captured, and a visually-hidden live region
 * announces each state to a screen reader. The one animation is the existing
 * `.session-cmp-pulse` — the console's established run-aliveness pulse, already
 * silenced under `prefers-reduced-motion` — rather than a new keyframe, and the
 * ticking clock keeps the recording legible when that motion is suppressed.
 *
 * Absent, not disabled, when the server advertises no transcription or the
 * browser cannot record: a disabled mic promises a capability that is not
 * coming back.
 */
/** Longest failure reason allowed into an accessible name. */
const MAX_REASON_CHARS = 120;

/**
 * Reduce a failure to something a screen reader can say, or to nothing.
 *
 * An upload does not only fail at the API: a proxy in front of it answers with
 * an HTML error page, and interpolating that raw into `aria-label` made the
 * button's entire accessible name a document — comment nodes included. So a
 * reason is used only when it is prose: anything carrying markup is dropped in
 * favour of the plain "Retry transcription", which is honest and short, and the
 * rest is bounded so no upstream can lengthen a label without limit.
 *
 * Cost class: `O(message length)`.
 */
function accessibleReason(error: string | null | undefined): string {
  const text = error?.trim();
  if (!text || text.includes('<')) return '';
  return text.length > MAX_REASON_CHARS ? `${text.slice(0, MAX_REASON_CHARS - 1)}…` : text;
}

export function MicButton({
  value,
  onChange,
  className = '',
  disabled = false,
}: {
  /** The composer's current text — the transcript is appended to it. */
  value: string;
  /** Receives the composer's text with the transcript appended. */
  onChange: (next: string) => void;
  className?: string;
  disabled?: boolean;
}) {
  const { canTranscribe, maxAudioBytes } = useSpeechCapability();
  const recorder = useAudioRecorder({
    // **A transcript is APPENDED, never a replacement**, and the rule lives
    // here so all four composers insert identically. Replacing would destroy
    // whatever the user had already typed with no way back; appending is always
    // recoverable by selecting the words and deleting them. It appends at the
    // END rather than at the caret because the four call sites expose three
    // different input elements (two textareas and a cmdk `Command.Input`) —
    // caret insertion would need a ref into each and would then behave
    // differently in the one that cannot give it.
    onTranscript: (text) => onChange(value.trim() ? `${value.replace(/\s+$/, '')} ${text}` : text),
    onError: (message) => toast.error(message),
    maxAudioBytes,
  });

  if (!canTranscribe || !recorder.supported) return null;

  const { status } = recorder;
  const live = status === 'recording' || status === 'paused';

  // The primary button keeps ONE seat in the toolbar across every state, so the
  // control never jumps sideways under the cursor mid-recording.
  const primary = {
    idle: {
      icon: <Mic className="h-4 w-4" />,
      name: 'Start voice input',
      onClick: recorder.start,
      tint: '',
    },
    recording: {
      icon: <Square className="h-4 w-4 fill-current" />,
      name: 'Stop recording and transcribe',
      onClick: recorder.stop,
      tint: 'text-[hsl(var(--destructive-text))]',
    },
    paused: {
      icon: <Square className="h-4 w-4 fill-current" />,
      name: 'Stop recording and transcribe',
      onClick: recorder.stop,
      tint: 'text-[hsl(var(--destructive-text))]',
    },
    transcribing: {
      icon: <Loader2 className="h-4 w-4 animate-spin" />,
      name: 'Transcribing your recording',
      onClick: undefined,
      tint: '',
    },
    failed: {
      icon: <RotateCcw className="h-4 w-4" />,
      name: accessibleReason(recorder.error)
        ? `Retry transcription — ${accessibleReason(recorder.error)}`
        : 'Retry transcription',
      onClick: recorder.retry,
      tint: 'text-[hsl(var(--destructive-text))]',
    },
  }[status];

  const announcement = {
    idle: '',
    recording: 'Recording',
    paused: 'Recording paused',
    transcribing: 'Transcribing your recording',
    failed: 'Transcription failed. Your recording was kept, so you can retry it.',
  }[status];

  return (
    <span className="inline-flex items-center gap-0.5">
      <span className="sr-only" role="status" aria-live="polite">
        {announcement}
      </span>

      {(live || status === 'failed') && (
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          tone="danger"
          onClick={live ? recorder.cancel : recorder.discard}
          aria-label={live ? 'Discard recording' : 'Discard recording without transcribing'}
          title={live ? 'Discard recording' : 'Discard recording without transcribing'}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      )}

      {live && recorder.canPause && (
        <Button
          variant="ghost"
          size="sm"
          iconOnly
          onClick={status === 'paused' ? recorder.resume : recorder.pause}
          aria-label={status === 'paused' ? 'Resume recording' : 'Pause recording'}
          title={status === 'paused' ? 'Resume recording' : 'Pause recording'}
        >
          {status === 'paused' ? (
            <Play className="h-3.5 w-3.5" />
          ) : (
            <Pause className="h-3.5 w-3.5" />
          )}
        </Button>
      )}

      {live && (
        // A duration is column-aligned machine text, so it keeps the mono face.
        <span className="inline-flex items-center gap-1 px-0.5" aria-hidden="true">
          <span
            className={cn(
              'h-1.5 w-1.5 rounded-full bg-[hsl(var(--destructive))]',
              status === 'recording' ? 'session-cmp-pulse' : 'opacity-40',
            )}
          />
          <span className="font-mono tabular-nums text-2xs text-[hsl(var(--muted-foreground))]">
            {formatElapsed(recorder.elapsedMs)}
          </span>
        </span>
      )}

      <Button
        variant="ghost"
        size="sm"
        iconOnly
        onClick={primary.onClick}
        disabled={disabled || status === 'transcribing'}
        aria-label={primary.name}
        title={primary.name}
        className={cn(primary.tint, className)}
      >
        {primary.icon}
      </Button>
    </span>
  );
}

/** `m:ss` — a dictation is minutes long at most, so no hours field. */
function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${minutes}:${String(seconds).padStart(2, '0')}`;
}
