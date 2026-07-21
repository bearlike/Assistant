/**
 * GitCredentialsView — manage product-wide git credentials.
 *
 * Chrome-agnostic (like ApiKeysView): renders a bare section card and owns no
 * page width/padding, so the Security settings facet supplies the shell. A
 * credential is keyed by a plain `scope`:
 *   - host scope (`git.example.com`) — shared by every repo on that host.
 *   - repo scope (`host/owner/repo`) — pinned to one repository.
 *
 * The raw secret is write-only: the list never carries it, and the Add/Edit
 * form (`GitCredentialDialog`) clears the value from state the moment a save
 * succeeds. This container owns the list + dialog-open state; the row
 * (`GitCredentialRow`) and the add/edit modal (`GitCredentialDialog`) are
 * self-contained siblings under `git-credentials/`.
 */
import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { Loader2, Plus, Trash2 } from 'lucide-react';

import { deleteGitCredential, type GitCredentialSummary } from '../api/git';
import { useGitCredentials } from '../hooks/useGitCredentials';
import { useWikiProjects } from './wiki/api/hooks';
import { Button } from './ui/button';
import { ErrorAlert } from './ErrorAlert';
import { SettingsCard } from './settings/SettingsCard';
import { ConfirmDialog } from './ui/confirm-dialog';
import { GitCredentialDialog, type GitCredentialDialogState } from './git-credentials/GitCredentialDialog';
import { GitCredentialRow } from './git-credentials/GitCredentialRow';

const CREDENTIALS_CARD_DESCRIPTION =
  'Tokens and SSH keys Mewbo uses to clone and fetch private repositories.\n\n' +
  '**Scope decides who shares a credential.** A bare host, `git.example.com` for instance, is ' +
  'used by every repository on that host. A full `host/owner/repo` slug is pinned to that one ' +
  'repository, and wins over a host credential when both match. Mewbo tries the most specific ' +
  "match first: the repository credential, then the host credential, then the server's own git " +
  'config.\n\n' +
  '**The value is write-only.** Once saved, the API returns only a short hint, never the secret ' +
  'itself, so editing a credential opens with an empty value field. That is not a bug: type a ' +
  'new value only if you want to replace the stored one.';

export function GitCredentialsView() {
  const { credentials, loading, error, refresh } = useGitCredentials();
  const wikiProjects = useWikiProjects();
  const projectSlugs = wikiProjects.data?.map((p) => p.slug) ?? [];
  const slugsLoaded = wikiProjects.data != null;

  const [dialogState, setDialogState] = useState<GitCredentialDialogState>(null);
  const [deleteTarget, setDeleteTarget] = useState<GitCredentialSummary | null>(null);

  const deleteM = useMutation({
    mutationFn: (scope: string) => deleteGitCredential(scope),
    onSuccess: () => {
      setDeleteTarget(null);
      refresh();
    },
  });

  const sharedCountFor = (cred: GitCredentialSummary): number | null => {
    if (cred.scopeType !== 'host' || !slugsLoaded) return null;
    const prefix = `${cred.scope}/`;
    return projectSlugs.filter((s) => s.startsWith(prefix)).length;
  };

  return (
    <div className="space-y-4">
      <SettingsCard
        title="Git credentials"
        description={CREDENTIALS_CARD_DESCRIPTION}
        actions={
          <Button
            variant="primary"
            size="md"
            onClick={() => setDialogState({ mode: 'add' })}
            leadingIcon={<Plus className="w-4 h-4" />}
          >
            Add credential
          </Button>
        }
      >
        {deleteM.error && (
          <ErrorAlert
            error={deleteM.error}
            fallback="Failed to delete credential"
            className="mb-2"
          />
        )}

        {error && <ErrorAlert error={error} fallback="Failed to load credentials" />}

        {loading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {!loading && !error && credentials.length === 0 && (
          <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
            <p className="text-sm text-[hsl(var(--muted-foreground))]">
              No git credentials stored yet.
            </p>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              Mewbo falls back to the git configuration on the server. Add a credential to reach
              private repositories it can&apos;t clone today, whether for wiki indexing or any
              other git flow.
            </p>
          </div>
        )}

        {!loading && credentials.length > 0 && (
          <div className="space-y-2">
            {credentials.map((cred) => (
              <GitCredentialRow
                key={cred.scope}
                cred={cred}
                sharedCount={sharedCountFor(cred)}
                onEdit={() => setDialogState({ mode: 'edit', cred })}
                onDelete={() => setDeleteTarget(cred)}
              />
            ))}
          </div>
        )}
      </SettingsCard>

      <GitCredentialDialog
        state={dialogState}
        onClose={() => setDialogState(null)}
        onSaved={refresh}
      />
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete credential?"
        description={
          <>
            This removes the stored credential for{' '}
            <span className="font-mono font-medium text-[hsl(var(--foreground))]">
              {deleteTarget?.scope}
            </span>
            .{' '}
            {deleteTarget?.scopeType === 'host'
              ? 'Every repository relying on this shared host credential will fall back to the ambient git credential.'
              : 'Git flows for this repository will fall back to a host credential or the ambient git credential.'}
          </>
        }
        confirmLabel="Delete"
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="w-4 h-4" />}
        pending={deleteM.isPending}
        onConfirm={() => deleteTarget && deleteM.mutate(deleteTarget.scope)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
