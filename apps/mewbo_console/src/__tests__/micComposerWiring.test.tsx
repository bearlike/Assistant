/**
 * The mic reaches BOTH composer families, from one hook and one control.
 *
 * The composer is only partly shared: `ComposerShell` backs wiki Q&A, agentic
 * search and apps, while the Tasks composer is a deliberate fork that shares
 * only the CSS chrome. So "build it once" is provable only by mounting one of
 * each and finding the SAME control — which is what this file does. Tasks is
 * additionally load-bearing because the mic there is not a new affordance: it
 * replaces a `<Mic>` icon that had rendered with no click handler at all, and a
 * regression that reverted it would look identical on screen.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { createRef } from 'react';

import { InputComposerBody } from '../components/InputComposerBody';
import { QADock } from '../components/wiki/QADock';

vi.mock('../components/wiki/ModelPicker', () => ({
  ModelPicker: () => <div data-testid="model-picker" />,
}));
vi.mock('../components/wiki/QaModeControl', () => ({
  QaModeControl: () => <div data-testid="qa-mode" />,
}));

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

beforeEach(() => {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => Promise.resolve(jsonResponse({ synthesis: true, transcription: true }))),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function mount(ui: React.ReactElement) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const TASKS_PROPS = {
  variant: 'detail' as const,
  inputValue: '',
  onInputChange: vi.fn(),
  onSubmit: vi.fn(),
  isSubmitting: false,
  expanded: false,
  queryMode: 'act' as const,
  onTogglePlanMode: vi.fn(),
  onAttach: vi.fn(),
  attachedFiles: [],
  onClearAttachments: vi.fn(),
  configMenu: null,
  modelSelector: null,
  textareaRef: createRef<HTMLTextAreaElement>(),
  placeholder: 'Ask anything',
  ariaLabel: 'Task composer',
};

describe('the mic reaches both composer families', () => {
  it('is live in the Tasks composer, where a dead icon used to sit', async () => {
    mount(<InputComposerBody {...TASKS_PROPS} />);

    const mic = await screen.findByRole('button', { name: /start voice input/i });
    // The affordance it replaced carried the static label "Voice input" and no
    // handler; a state-naming label is the tell that the live one is mounted.
    expect(mic).toBeEnabled();
    expect(screen.queryByRole('button', { name: /^voice input$/i })).toBeNull();
  });

  it('is live in a ComposerShell surface (wiki Q&A)', async () => {
    mount(
      <QADock
        placeholder="Ask the wiki"
        model="gpt-5"
        onModelChange={vi.fn()}
        mode="fast"
        onModeChange={vi.fn()}
        onAsk={vi.fn()}
      />,
    );

    expect(await screen.findByRole('button', { name: /start voice input/i })).toBeEnabled();
  });

  it('stays out of the Tasks composer while a run is in flight', async () => {
    mount(<InputComposerBody {...TASKS_PROPS} isRunning showStop />);

    // Mid-run the composer narrows to a steering instrument; dictation is not
    // part of that, and the Stop control takes the seat instead.
    expect(await screen.findByRole('button', { name: /stop run/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start voice input/i })).toBeNull();
  });
});
