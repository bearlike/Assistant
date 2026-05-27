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
import { cn } from '../lib/utils';
import { sectionTitleCls } from './settings/styles';
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

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatDate(iso: string): string {
  try {
    return new Intl.DateTimeFormat(undefined, {
      dateStyle: 'medium',
      timeStyle: 'short',
    }).format(new Date(iso));
  } catch {
    return iso;
  }
}

function InlineError({ error, fallback }: { error: unknown; fallback: string }) {
  const message = error instanceof Error ? error.message : fallback;
  return (
    <div className="flex items-start gap-2 rounded-lg border border-[hsl(var(--destructive))]/30 bg-[hsl(var(--destructive))]/10 px-3 py-2.5">
      <AlertTriangle className="w-4 h-4 text-[hsl(var(--destructive))] shrink-0 mt-0.5" />
      <p className="text-xs text-[hsl(var(--destructive))]">{message}</p>
    </div>
  );
}

const createKeySchema = z.object({
  label: z.string().trim().min(1, 'Label is required'),
});

type CreateKeyValues = z.infer<typeof createKeySchema>;

// ---------------------------------------------------------------------------
// Revoke-confirm dialog
// ---------------------------------------------------------------------------

interface RevokeDialogProps {
  keyItem: ApiKeySummary | null;
  onConfirm: () => void;
  onCancel: () => void;
  revoking: boolean;
}

function RevokeDialog({ keyItem, onConfirm, onCancel, revoking }: RevokeDialogProps) {
  return (
    <Dialog open={keyItem !== null} onOpenChange={(open) => { if (!open) onCancel(); }}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle>Revoke API key?</DialogTitle>
          <DialogDescription>
            This will permanently revoke{' '}
            <span className="font-medium text-[hsl(var(--foreground))]">
              {keyItem?.label || 'this key'}
            </span>
            . Any application using it will immediately lose access. This action cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="ghost" size="md" onClick={onCancel} disabled={revoking}>
            Cancel
          </Button>
          <Button
            variant="neutral"
            tone="danger"
            size="md"
            onClick={onConfirm}
            disabled={revoking}
            leadingIcon={
              revoking ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Trash2 className="w-4 h-4" />
              )
            }
          >
            {revoking ? 'Revoking…' : 'Revoke key'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

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
            Copy this key now — it will not be shown again.
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
      <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium leading-none bg-[hsl(var(--destructive))]/15 text-[hsl(var(--destructive))] border border-[hsl(var(--destructive))]/20">
        Revoked
      </span>
    );
  }
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 rounded-full text-[10px] font-medium leading-none bg-[hsl(var(--success))]/15 text-[hsl(var(--success))] border border-[hsl(var(--success))]/20">
      Active
    </span>
  );
}

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
      <section className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5">
        <h2 className={cn(sectionTitleCls, "mb-3")}>Create a new key</h2>
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
          <div className="mt-2">
            <InlineError error={createM.error} fallback="Failed to create key" />
          </div>
        )}
      </section>

      {/* Keys list */}
      <section className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-5">
        <h2 className={cn(sectionTitleCls, "mb-3")}>
          Issued keys
          {keys.length > 0 && (
            <span className="ml-2 text-xs font-normal text-[hsl(var(--muted-foreground))]">
              ({keys.length})
            </span>
          )}
        </h2>

        {revokeM.error && (
          <div className="mb-2">
            <InlineError error={revokeM.error} fallback="Failed to revoke key" />
          </div>
        )}

        {listError && (
          <InlineError error={listError} fallback="Failed to load keys" />
        )}

        {listLoading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {!listLoading && !listError && keys.length === 0 && (
          <p className="text-sm text-[hsl(var(--muted-foreground))]">No keys issued yet.</p>
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
                        Created {formatDate(k.created_at)}
                      </span>
                      {k.revoked_at && (
                        <span className="text-xs text-[hsl(var(--muted-foreground))]">
                          Revoked {formatDate(k.revoked_at)}
                        </span>
                      )}
                      <span className="text-[10px] font-mono text-[hsl(var(--muted-foreground))]">
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
      </section>

      {/* Dialogs */}
      <NewKeyDialog
        plaintext={newKeyPlaintext}
        onClose={() => setNewKeyPlaintext(null)}
      />
      <RevokeDialog
        keyItem={revokeTarget}
        onConfirm={handleRevokeConfirm}
        onCancel={() => setRevokeTarget(null)}
        revoking={revokeM.isPending}
      />
    </div>
  );
}
