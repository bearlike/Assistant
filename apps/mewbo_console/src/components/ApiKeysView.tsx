import { useState } from 'react';
import { useQueryClient, useQuery, useMutation } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import {
  Plus,
  Trash2,
  Loader2,
  AlertTriangle,
} from 'lucide-react';
import { listApiKeys, createApiKey, revokeApiKey } from '../api/client';
import type { ApiKeySummary } from '../api/client';
import { Button } from './ui/button';
import { Input } from './ui/input';
import { CopyButton } from './CopyButton';
import { ErrorAlert } from './ErrorAlert';
import { SettingsCard } from './settings/SettingsCard';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormMessage,
} from './ui/form';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from './ui/dialog';
import { ConfirmDialog } from './ui/confirm-dialog';
import { formatDateTime } from '../utils/time';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const createKeySchema = z.object({
  label: z.string().trim().min(1, 'Label is required'),
});

type CreateKeyValues = z.infer<typeof createKeySchema>;

// ---------------------------------------------------------------------------
// Show-new-key dialog
// ---------------------------------------------------------------------------

interface NewKeyDialogProps {
  plaintext: string | null;
  onClose: () => void;
}

function NewKeyDialog({ plaintext, onClose }: NewKeyDialogProps) {
  return (
    <Dialog open={plaintext !== null} onOpenChange={(open) => { if (!open) onClose(); }}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>API key created</DialogTitle>
          <DialogDescription>
            Copy this key now. It will not be shown again.
          </DialogDescription>
        </DialogHeader>

        {plaintext && (
          <div className="space-y-3">
            <div className="flex items-center gap-2 rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 px-3 py-2">
              <code className="flex-1 text-xs font-mono text-[hsl(var(--foreground))] break-all select-all">
                {plaintext}
              </code>
              <CopyButton text={plaintext} className="shrink-0">Copy</CopyButton>
            </div>

            <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--warning))]/30 bg-[hsl(var(--warning))]/10 px-3 py-2.5">
              <AlertTriangle className="w-4 h-4 text-[hsl(var(--warning))] shrink-0 mt-0.5" />
              <p className="text-xs text-[hsl(var(--warning))]">
                This is the only time the plaintext key is shown. Store it securely before closing this dialog.
              </p>
            </div>
          </div>
        )}

        <DialogFooter>
          <Button variant="primary" size="md" onClick={onClose}>
            Done
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

// ---------------------------------------------------------------------------
// Key status pill — `rounded-full` per shape vocabulary (state container)
// ---------------------------------------------------------------------------

function KeyStatusPill({ revoked }: { revoked: boolean }) {
  if (revoked) {
    return (
      <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-2xs font-medium leading-none bg-[hsl(var(--destructive))]/15 text-[hsl(var(--destructive-text))] border border-[hsl(var(--destructive))]/20">
        Revoked
      </span>
    );
  }
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-2xs font-medium leading-none bg-[hsl(var(--success))]/15 text-[hsl(var(--success))] border border-[hsl(var(--success))]/20">
      Active
    </span>
  );
}

// ---------------------------------------------------------------------------
// Card copy
// ---------------------------------------------------------------------------

const CREATE_CARD_DESCRIPTION =
  'A key authenticates calls to the REST API and the MCP server on your behalf.\n\n' +
  'The key is shown in full exactly once, right after you create it. Copy it then, because ' +
  'Mewbo cannot show it to you again.\n\n' +
  'Give CI and other automation their own labeled key rather than the API master token ' +
  '(Server & Storage → API Server). A labeled key can be revoked on its own, without locking ' +
  'every other caller out.';

const ISSUED_CARD_DESCRIPTION =
  'Revoking a key takes effect immediately and cannot be undone. Anything still using it loses access at once.';

// ---------------------------------------------------------------------------
// Main view
// ---------------------------------------------------------------------------

export function ApiKeysView() {
  const qc = useQueryClient();

  // List query
  const {
    data: keys = [],
    isPending: listLoading,
    error: listError,
  } = useQuery({
    queryKey: ['api-keys'],
    queryFn: listApiKeys,
    staleTime: 30_000,
  });

  // Create form (react-hook-form + zod) + mutation
  const [newKeyPlaintext, setNewKeyPlaintext] = useState<string | null>(null);
  const form = useForm<CreateKeyValues>({
    resolver: zodResolver(createKeySchema),
    defaultValues: { label: '' },
  });
  const createM = useMutation({
    mutationFn: (label: string) => createApiKey(label),
    onSuccess: (result) => {
      form.reset({ label: '' });
      setNewKeyPlaintext(result.key);
      void qc.invalidateQueries({ queryKey: ['api-keys'] });
    },
  });

  const handleCreate = form.handleSubmit((values) => {
    createM.mutate(values.label.trim());
  });

  // Revoke mutation
  const [revokeTarget, setRevokeTarget] = useState<ApiKeySummary | null>(null);
  const revokeM = useMutation({
    mutationFn: (id: string) => revokeApiKey(id),
    onSuccess: () => {
      setRevokeTarget(null);
      void qc.invalidateQueries({ queryKey: ['api-keys'] });
    },
  });

  const handleRevokeConfirm = () => {
    if (revokeTarget) {
      revokeM.mutate(revokeTarget.id);
    }
  };

  return (
    <div className="space-y-4">

      {/* Create new key */}
      <SettingsCard title="Create a new key" description={CREATE_CARD_DESCRIPTION}>
        <Form {...form}>
          <form onSubmit={handleCreate} className="flex items-start gap-2">
            <FormField
              control={form.control}
              name="label"
              render={({ field }) => (
                <FormItem className="flex-1 space-y-1">
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="Key label (e.g. my-agent)"
                      disabled={createM.isPending}
                      aria-label="New key label"
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />
            <Button
              type="submit"
              variant="primary"
              size="md"
              disabled={createM.isPending}
              leadingIcon={
                createM.isPending ? (
                  <Loader2 className="w-4 h-4 animate-spin" />
                ) : (
                  <Plus className="w-4 h-4" />
                )
              }
            >
              {createM.isPending ? 'Creating…' : 'Create key'}
            </Button>
          </form>
        </Form>

        {createM.error && (
          <ErrorAlert
            error={createM.error}
            fallback="Failed to create key"
            className="mt-2"
          />
        )}
      </SettingsCard>

      {/* Keys list */}
      <SettingsCard
        title={keys.length > 0 ? `Issued keys (${keys.length})` : 'Issued keys'}
        description={ISSUED_CARD_DESCRIPTION}
      >
        {revokeM.error && (
          <ErrorAlert
            error={revokeM.error}
            fallback="Failed to revoke key"
            className="mb-2"
          />
        )}

        {listError && <ErrorAlert error={listError} fallback="Failed to load keys" />}

        {listLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {!listLoading && !listError && keys.length === 0 && (
          <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
            <p className="text-sm text-[hsl(var(--muted-foreground))]">No keys issued yet.</p>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              Create one above for each caller, whether that is a CI job, a script, or an MCP
              client, so you can revoke that one without cutting off the rest.
            </p>
          </div>
        )}

        {!listLoading && keys.length > 0 && (
          <div className="space-y-2">
            {keys.map((k) => {
              const isRevoked = Boolean(k.revoked_at);
              return (
                <div
                  key={k.id}
                  className="flex items-start justify-between gap-4 rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-4 py-3"
                >
                  <div className="min-w-0 flex-1 space-y-1">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-[hsl(var(--foreground))]">
                        {k.label}
                      </span>
                      <KeyStatusPill revoked={isRevoked} />
                    </div>
                    <div className="flex items-center gap-3 flex-wrap">
                      <span className="text-xs text-[hsl(var(--muted-foreground))]">
                        Created {formatDateTime(k.created_at)}
                      </span>
                      {k.revoked_at && (
                        <span className="text-xs text-[hsl(var(--muted-foreground))]">
                          Revoked {formatDateTime(k.revoked_at)}
                        </span>
                      )}
                      <span className="text-2xs font-mono text-[hsl(var(--muted-foreground))]">
                        id:{k.id}
                      </span>
                    </div>
                  </div>

                  {!isRevoked && (
                    <Button
                      variant="neutral"
                      size="sm"
                      tone="danger"
                      onClick={() => setRevokeTarget(k)}
                      disabled={revokeM.isPending && revokeTarget?.id === k.id}
                      aria-label={`Revoke ${k.label}`}
                      leadingIcon={<Trash2 className="w-3.5 h-3.5" />}
                      className="shrink-0"
                    >
                      Revoke
                    </Button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </SettingsCard>

      {/* Dialogs */}
      <NewKeyDialog
        plaintext={newKeyPlaintext}
        onClose={() => setNewKeyPlaintext(null)}
      />
      <ConfirmDialog
        open={revokeTarget !== null}
        title="Revoke API key?"
        description={
          <>
            This will permanently revoke{' '}
            <span className="font-medium text-[hsl(var(--foreground))]">
              {revokeTarget?.label || 'this key'}
            </span>
            . Any application using it will immediately lose access. This action cannot be undone.
          </>
        }
        confirmLabel="Revoke key"
        pendingLabel="Revoking…"
        confirmIcon={<Trash2 className="w-4 h-4" />}
        pending={revokeM.isPending}
        onConfirm={handleRevokeConfirm}
        onCancel={() => setRevokeTarget(null)}
      />
    </div>
  );
}
