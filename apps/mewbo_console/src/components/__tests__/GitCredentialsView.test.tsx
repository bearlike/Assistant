/**
 * GitCredentialsView — render test.
 *
 * This pane had ZERO coverage before this test. It exercises the three
 * things most likely to silently regress:
 *   - it mounts inside a QueryClientProvider and lists every stored
 *     credential row (`useGitCredentials` + `api/git.ts` mocked);
 *   - host vs repo `scopeType` renders as visually distinct chips
 *     ("Shared host" vs "Repository") — the distinction the task brief
 *     calls out as the thing users get wrong;
 *   - the write-only `value` NEVER round-trips into the edit form, even
 *     though a credential already exists for that scope — the API only
 *     ever returns a `valueHint`, never the secret itself, so the Value
 *     field must stay blank on Edit.
 */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { GitCredentialsView } from '../GitCredentialsView';
import type { GitCredentialSummary } from '../../api/git';

const HOST_CRED: GitCredentialSummary = {
  scope: 'git.example.com',
  scopeType: 'host',
  kind: 'token',
  username: null,
  valueHint: '…abcd',
  updatedAt: '2026-07-01T00:00:00Z',
};

const REPO_CRED: GitCredentialSummary = {
  scope: 'git.example.com/acme/widgets',
  scopeType: 'repo',
  kind: 'ssh_key',
  username: 'git',
  valueHint: 'ssh key',
  updatedAt: null,
};

vi.mock('../../hooks/useGitCredentials', () => ({
  useGitCredentials: () => ({
    credentials: [HOST_CRED, REPO_CRED],
    loading: false,
    error: null,
    refresh: vi.fn(),
  }),
}));

// The onboarding-hint query GitCredentialsView reads for "shared across N
// projects" — inert here, no wiki projects fetch needed for this coverage.
vi.mock('../wiki/api/hooks', () => ({
  useWikiProjects: () => ({ data: [] }),
}));

// Keep the real `scopeTypeOf`/`hostOf` helpers (the dialog's live scope-type
// chip depends on them); stub only the network calls.
vi.mock('../../api/git', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/git')>();
  return {
    ...actual,
    putGitCredential: vi.fn(),
    deleteGitCredential: vi.fn(),
    validateGitCredential: vi.fn(),
  };
});

function renderView() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <GitCredentialsView />
    </QueryClientProvider>
  );
}

afterEach(cleanup);
beforeEach(() => {
  vi.clearAllMocks();
});

describe('GitCredentialsView', () => {
  it('mounts and renders every stored credential row', () => {
    renderView();
    expect(screen.getByRole('heading', { name: 'Git credentials' })).toBeInTheDocument();
    expect(screen.getByText(HOST_CRED.scope)).toBeInTheDocument();
    expect(screen.getByText(REPO_CRED.scope)).toBeInTheDocument();
  });

  it('shows the host-vs-repo scope distinctly', () => {
    renderView();
    expect(screen.getByText('Shared host')).toBeInTheDocument();
    expect(screen.getByText('Repository')).toBeInTheDocument();
  });

  it('never round-trips the write-only value into the edit form', async () => {
    const user = userEvent.setup();
    renderView();

    await user.click(
      screen.getByRole('button', { name: `Edit credential for ${HOST_CRED.scope}` })
    );

    const dialog = await screen.findByRole('dialog', { name: 'Update credential' });

    // Scope is prefilled (read-only identity on edit)...
    expect(within(dialog).getByDisplayValue(HOST_CRED.scope)).toBeInTheDocument();
    // ...but the secret itself is never sent back by the API, so Value is blank.
    const valueInput = within(dialog).getByPlaceholderText('Enter a new token');
    expect(valueInput).toHaveValue('');
  });
});
