/**
 * The dictation control, driven through its real hook and its real API module.
 *
 * Only two things are stubbed and both are I/O boundaries: `fetch` (so a request
 * is observable and no server is needed) and the browser's capture APIs (jsdom
 * has neither, and `setupTests.ts` installs the fakes). Everything between —
 * the state machine, the multipart body, the filename extension, the append
 * rule, the gating — is production code.
 *
 * Two assertions here are product requirements rather than implementation
 * details, and they are the reason this file exists:
 *
 *   - **Cancel issues ZERO requests.** A discarded recording that still billed a
 *     transcription defeats the entire purpose of the control.
 *   - **Every exit stops the media tracks.** A track left live keeps the
 *     browser's recording indicator on after the UI says recording ended.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useState } from 'react';

import { MicButton } from '../components/MicButton';
import { fileNameFor } from '../hooks/useAudioRecorder';

// The one fake `setupTests.ts` installs; typed here rather than exported from
// there so the stub stays an implementation detail of the test environment.
interface RecorderStub {
  state: 'inactive' | 'recording' | 'paused';
  mimeType: string;
  stop: () => void;
}
interface RecorderCtor {
  instances: RecorderStub[];
  supportedTypes: string[];
}

function recorderCtor(): RecorderCtor {
  return globalThis.MediaRecorder as unknown as RecorderCtor;
}

function latestRecorder(): RecorderStub {
  const { instances } = recorderCtor();
  const recorder = instances[instances.length - 1];
  if (!recorder) throw new Error('no MediaRecorder was constructed');
  return recorder;
}

/** Requests the component actually issued, in order. */
function requests(): Array<{ url: string; init?: RequestInit }> {
  return (fetch as unknown as { mock: { calls: [string, RequestInit?][] } }).mock.calls.map(
    ([url, init]) => ({ url: String(url), init }),
  );
}

function transcribeCalls() {
  return requests().filter((call) => call.url.includes('/transcribe'));
}

const CAPABILITIES_OK = { synthesis: true, transcription: true };

function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

/** Route by URL so capability and transcribe can answer differently. */
function routeFetch(handlers: {
  capabilities?: unknown;
  transcribe?: () => Promise<Response>;
}) {
  return vi.fn((url: string | URL) => {
    const href = String(url);
    if (href.includes('/capabilities')) {
      return Promise.resolve(jsonResponse(handlers.capabilities ?? CAPABILITIES_OK));
    }
    if (href.includes('/transcribe')) {
      return (handlers.transcribe ?? (() => Promise.resolve(jsonResponse({ text: 'hello there' }))))();
    }
    return Promise.reject(new Error(`unexpected fetch: ${href}`));
  });
}

/** Mounts the control over a real controlled input, as every composer does. */
function Harness({ initial = '' }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <>
      <textarea aria-label="composer" value={value} onChange={(e) => setValue(e.target.value)} />
      <MicButton value={value} onChange={setValue} />
    </>
  );
}

function renderMic(ui: React.ReactElement) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** The mic only appears once the capability query has answered. */
async function record(user: ReturnType<typeof userEvent.setup>) {
  const start = await screen.findByRole('button', { name: /start voice input/i });
  await user.click(start);
  return screen.findByRole('button', { name: /stop recording and transcribe/i });
}

let trackStop: ReturnType<typeof vi.fn>;

beforeEach(() => {
  recorderCtor().instances.length = 0;
  recorderCtor().supportedTypes = ['audio/webm;codecs=opus', 'audio/webm'];
  trackStop = vi.fn();
  vi.spyOn(navigator.mediaDevices, 'getUserMedia').mockResolvedValue({
    getTracks: () => [{ stop: trackStop }],
  } as unknown as MediaStream);
  vi.stubGlobal('fetch', routeFetch({}));
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('MicButton — availability gating', () => {
  it('renders nothing until the server advertises transcription', async () => {
    vi.stubGlobal('fetch', routeFetch({ capabilities: { synthesis: true, transcription: false } }));
    renderMic(<Harness />);

    // Give the capability query time to resolve, then confirm it stayed hidden
    // — a bare "not present" would also pass before the query answers.
    await waitFor(() => expect(requests().some((r) => r.url.includes('/capabilities'))).toBe(true));
    expect(screen.queryByRole('button', { name: /voice input/i })).toBeNull();
  });

  it('stays hidden when the capability probe fails outright', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.reject(new Error('speech namespace not mounted'))),
    );
    renderMic(<Harness />);

    await waitFor(() => expect(requests().length).toBeGreaterThan(0));
    expect(screen.queryByRole('button', { name: /voice input/i })).toBeNull();
  });

  it('accepts the richer nested capability shape as well as bare booleans', async () => {
    vi.stubGlobal(
      'fetch',
      routeFetch({
        capabilities: {
          synthesis: { available: true, model: 'supertonic-3' },
          transcription: { available: true, model: 'nova-3', limits: { max_audio_bytes: 10485760 } },
        },
      }),
    );
    renderMic(<Harness />);

    expect(await screen.findByRole('button', { name: /start voice input/i })).toBeInTheDocument();
  });
});

describe('MicButton — the state machine', () => {
  it('walks idle → recording → paused → recording → transcribing → idle', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    expect(latestRecorder().state).toBe('recording');
    expect(screen.getByRole('status')).toHaveTextContent('Recording');

    await user.click(screen.getByRole('button', { name: /pause recording/i }));
    expect(latestRecorder().state).toBe('paused');
    expect(screen.getByRole('status')).toHaveTextContent('Recording paused');

    await user.click(screen.getByRole('button', { name: /resume recording/i }));
    expect(latestRecorder().state).toBe('recording');
    expect(screen.getByRole('status')).toHaveTextContent('Recording');

    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));
    expect(latestRecorder().state).toBe('inactive');

    // Back to idle once the transcript lands.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /start voice input/i })).toBeInTheDocument(),
    );
  });

  it('shows a loading indicator on the record button while transcribing', async () => {
    const user = userEvent.setup();
    let release: (value: Response) => void = () => undefined;
    vi.stubGlobal(
      'fetch',
      routeFetch({ transcribe: () => new Promise<Response>((resolve) => (release = resolve)) }),
    );
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    const spinner = await screen.findByRole('button', { name: /transcribing your recording/i });
    expect(spinner).toBeDisabled();
    expect(screen.getByRole('status')).toHaveTextContent('Transcribing');

    release(jsonResponse({ text: 'done' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /start voice input/i })).toBeInTheDocument(),
    );
  });
});

describe('MicButton — cancel costs nothing', () => {
  it('fires NO network request when a recording is cancelled', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /discard recording/i }));

    // The recorder's stop handler runs in a microtask; let it, then confirm it
    // discarded rather than uploaded. Without this wait the assertion would
    // pass simply because nothing had happened yet.
    await waitFor(() => expect(latestRecorder().state).toBe('inactive'));
    await Promise.resolve();
    expect(transcribeCalls()).toHaveLength(0);
    expect(screen.getByRole('button', { name: /start voice input/i })).toBeInTheDocument();
  });

  it('fires NO network request when a PAUSED recording is cancelled', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /pause recording/i }));
    await user.click(screen.getByRole('button', { name: /discard recording/i }));

    await waitFor(() => expect(latestRecorder().state).toBe('inactive'));
    await Promise.resolve();
    expect(transcribeCalls()).toHaveLength(0);
  });

  it('releases the media tracks on cancel', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    expect(trackStop).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: /discard recording/i }));
    await waitFor(() => expect(trackStop).toHaveBeenCalled());
  });

  it('releases the media tracks when the composer unmounts mid-recording', async () => {
    const user = userEvent.setup();
    const { unmount } = renderMic(<Harness />);

    await record(user);
    expect(trackStop).not.toHaveBeenCalled();

    unmount();
    await waitFor(() => expect(trackStop).toHaveBeenCalled());
    await Promise.resolve();
    expect(transcribeCalls()).toHaveLength(0);
  });
});

describe('MicButton — upload and transcript', () => {
  it('uploads exactly once, as multipart with a `file` part named by container', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    await waitFor(() => expect(transcribeCalls()).toHaveLength(1));
    const [call] = transcribeCalls();
    expect(call.url).toContain('/api/speech/transcribe');
    expect(call.init?.method).toBe('POST');

    const body = call.init?.body as FormData;
    expect(body).toBeInstanceOf(FormData);
    const file = body.get('file') as File;
    // The server reads the EXTENSION as its format hint, so this is a wire
    // contract, not a cosmetic filename.
    expect(file.name).toBe('recording.webm');

    // The multipart `Content-Type` must be left to the browser — spelling it
    // here would omit the boundary and the server would parse zero parts.
    const headers = new Headers(call.init?.headers);
    expect(headers.get('content-type')).toBeNull();
  });

  it('appends the transcript to existing composer text rather than replacing it', async () => {
    const user = userEvent.setup();
    vi.stubGlobal('fetch', routeFetch({ transcribe: () => Promise.resolve(jsonResponse({ text: 'and then some' })) }));
    renderMic(<Harness initial="typed already" />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    await waitFor(() =>
      expect(screen.getByLabelText('composer')).toHaveValue('typed already and then some'),
    );
  });

  it('uses the transcript alone when the composer was empty', async () => {
    const user = userEvent.setup();
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    await waitFor(() => expect(screen.getByLabelText('composer')).toHaveValue('hello there'));
  });

  it('maps each recorded container to the extension the gateway expects', () => {
    expect(fileNameFor('audio/webm;codecs=opus')).toBe('recording.webm');
    expect(fileNameFor('audio/mp4')).toBe('recording.mp4');
    expect(fileNameFor('audio/wav')).toBe('recording.wav');
    // An unknown container still gets a plausible extension rather than none.
    expect(fileNameFor('audio/something-new')).toBe('recording.webm');
  });
});

describe('MicButton — failure paths', () => {
  it('surfaces a denied microphone without wedging, and keeps no stream', async () => {
    const user = userEvent.setup();
    const denial = Object.assign(new Error('denied'), { name: 'NotAllowedError' });
    vi.spyOn(navigator.mediaDevices, 'getUserMedia').mockRejectedValue(denial);
    renderMic(<Harness />);

    const start = await screen.findByRole('button', { name: /start voice input/i });
    await user.click(start);

    // Still idle and still clickable — no spinner, no dead control.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: /start voice input/i })).toBeEnabled(),
    );
    expect(screen.queryByRole('button', { name: /transcribing/i })).toBeNull();
    expect(transcribeCalls()).toHaveLength(0);
  });

  it('keeps the recording after a failed transcription so it can be retried', async () => {
    const user = userEvent.setup();
    let attempt = 0;
    vi.stubGlobal(
      'fetch',
      routeFetch({
        transcribe: () => {
          attempt += 1;
          return attempt === 1
            ? Promise.resolve(
                jsonResponse(
                  { error: { code: 'speech_gateway_error', reason: 'Deepgram auth failed', retryable: true } },
                  502,
                ),
              )
            : Promise.resolve(jsonResponse({ text: 'recovered words' }));
        },
      }),
    );
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    const retry = await screen.findByRole('button', { name: /retry transcription/i });
    expect(screen.getByRole('status')).toHaveTextContent('Your recording was kept');

    await user.click(retry);

    // The SAME audio is re-sent — the user never re-records.
    await waitFor(() => expect(transcribeCalls()).toHaveLength(2));
    await waitFor(() => expect(screen.getByLabelText('composer')).toHaveValue('recovered words'));
  });

  it('can discard a failed recording instead of retrying, sending nothing more', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      routeFetch({
        transcribe: () =>
          Promise.resolve(
            jsonResponse({ error: { code: 'speech_gateway_error', reason: 'gateway down', retryable: true } }, 502),
          ),
      }),
    );
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));
    await screen.findByRole('button', { name: /retry transcription/i });

    await user.click(screen.getByRole('button', { name: /discard recording without transcribing/i }));

    expect(await screen.findByRole('button', { name: /start voice input/i })).toBeInTheDocument();
    expect(transcribeCalls()).toHaveLength(1);
  });

  it('refuses a recording over the server-published size cap without uploading it', async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      'fetch',
      routeFetch({
        capabilities: {
          synthesis: true,
          transcription: { available: true, limits: { max_audio_bytes: 1 } },
        },
      }),
    );
    renderMic(<Harness />);

    await record(user);
    await user.click(screen.getByRole('button', { name: /stop recording and transcribe/i }));

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /start voice input/i })).toBeInTheDocument(),
    );
    expect(transcribeCalls()).toHaveLength(0);
  });
});
