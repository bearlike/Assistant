/**
 * GitCredentialDialog — the add/edit modal for `GitCredentialsView`.
 *
 * The `value` field is ALWAYS seeded empty, even on edit: the API only ever
 * returns a `valueHint`, never the secret itself, so there is nothing to
 * round-trip. `form.reset(EMPTY_FORM)` on success additionally drops the raw
 * secret from client state the moment the save succeeds.
 */
import { useEffect } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useForm } from 'react-hook-form';
import { zodResolver } from '@hookform/resolvers/zod';
import { z } from 'zod';
import { Loader2, ShieldCheck } from 'lucide-react';

import {
  putGitCredential,
  scopeTypeOf,
  type GitCredentialSummary,
} from '../../api/git';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Textarea } from '../ui/textarea';
import { ErrorAlert } from '../ErrorAlert';
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from '../ui/form';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../ui/dialog';
import { inputBase } from '../settings/styles';
import { ScopeTypeChip } from './GitCredentialChips';

const credentialSchema = z.object({
  scope: z.string().trim().min(1, 'Scope is required'),
  kind: z.enum(['token', 'ssh_key']),
  value: z.string().min(1, 'Value is required'),
  username: z.string().trim().optional(),
});

type CredentialValues = z.infer<typeof credentialSchema>;

export type GitCredentialDialogState =
  | { mode: 'add' | 'edit'; cred?: GitCredentialSummary }
  | null;

const EMPTY_FORM: CredentialValues = { scope: '', kind: 'token', value: '', username: '' };

export function GitCredentialDialog({
  state,
  onClose,
  onSaved,
}: {
  state: GitCredentialDialogState;
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = state?.mode === 'edit';
  const form = useForm<CredentialValues>({
    resolver: zodResolver(credentialSchema),
    defaultValues: EMPTY_FORM,
  });

  // Seed the form each time the dialog opens (edit prefills scope/kind/username;
  // the value is ALWAYS empty — the secret is never returned by the API).
  useEffect(() => {
    if (state) {
      form.reset({
        scope: state.cred?.scope ?? '',
        kind: state.cred?.kind ?? 'token',
        value: '',
        username: state.cred?.username ?? '',
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  const putM = useMutation({
    mutationFn: (values: CredentialValues) =>
      putGitCredential(values.scope.trim(), {
        kind: values.kind,
        value: values.value,
        username: values.username?.trim() ? values.username.trim() : undefined,
      }),
    onSuccess: () => {
      form.reset(EMPTY_FORM); // drops the raw secret from client state
      onSaved();
      onClose();
    },
  });

  const kind = form.watch('kind');
  const scopeValue = form.watch('scope');
  const scopeType = scopeTypeOf(scopeValue);

  const handleSubmit = form.handleSubmit((values) => putM.mutate(values));

  const close = () => {
    form.reset(EMPTY_FORM);
    onClose();
  };

  return (
    <Dialog open={state !== null} onOpenChange={(open) => { if (!open) close(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Update credential' : 'Add git credential'}</DialogTitle>
          <DialogDescription>
            Used whenever Mewbo clones or fetches, for wiki indexing, repository search, and the
            agent&apos;s own git flows.
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Scope */}
            <FormField
              control={form.control}
              name="scope"
              render={({ field }) => (
                <FormItem className="space-y-1.5">
                  <div className="flex items-center gap-2">
                    <FormLabel>Scope</FormLabel>
                    {scopeValue.trim() && <ScopeTypeChip scopeType={scopeType} />}
                  </div>
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="git.example.com  or  git.example.com/owner/repo"
                      disabled={isEdit || putM.isPending}
                      autoComplete="off"
                      spellCheck={false}
                      className="font-mono"
                    />
                  </FormControl>
                  <p className="text-xs text-[hsl(var(--muted-foreground))]">
                    A bare host like{' '}
                    <code className="font-mono text-[hsl(var(--foreground))]">git.example.com</code>{' '}
                    shares the credential with every repository on that host;{' '}
                    <code className="font-mono text-[hsl(var(--foreground))]">host/owner/repo</code>{' '}
                    pins it to one repository.
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Kind — tiny static option set → native select (KISS), styled
                with the surface's `inputBase` tokens like every other select
                on this surface (RecordListField's type picker, RJSF's
                enum SelectWidget) rather than shadcn-default border/ring. */}
            <FormField
              control={form.control}
              name="kind"
              render={({ field }) => (
                <FormItem className="space-y-1.5">
                  <FormLabel>Kind</FormLabel>
                  <FormControl>
                    <select
                      {...field}
                      disabled={putM.isPending}
                      className={inputBase}
                    >
                      <option value="token">Access token</option>
                      <option value="ssh_key">SSH private key</option>
                    </select>
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Value — password for tokens, textarea for ssh keys. */}
            <FormField
              control={form.control}
              name="value"
              render={({ field }) => (
                <FormItem className="space-y-1.5">
                  <FormLabel>{kind === 'ssh_key' ? 'Private key' : 'Token'}</FormLabel>
                  <FormControl>
                    {kind === 'ssh_key' ? (
                      <Textarea
                        {...field}
                        rows={5}
                        placeholder={'-----BEGIN OPENSSH PRIVATE KEY-----\n…'}
                        disabled={putM.isPending}
                        autoComplete="off"
                        spellCheck={false}
                        className="font-mono text-field md:text-sm"
                      />
                    ) : (
                      <Input
                        {...field}
                        type="password"
                        placeholder={isEdit ? 'Enter a new token' : 'Paste access token'}
                        disabled={putM.isPending}
                        autoComplete="off"
                      />
                    )}
                  </FormControl>
                  {isEdit && (
                    <p className="text-xs text-[hsl(var(--muted-foreground))]">
                      Re-enter the value to update it. The stored secret is never shown.
                    </p>
                  )}
                  <FormMessage />
                </FormItem>
              )}
            />

            {/* Username (optional) */}
            <FormField
              control={form.control}
              name="username"
              render={({ field }) => (
                <FormItem className="space-y-1.5">
                  <FormLabel>Username (optional)</FormLabel>
                  <FormControl>
                    <Input
                      {...field}
                      type="text"
                      placeholder="x-access-token"
                      disabled={putM.isPending}
                      autoComplete="off"
                    />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            {putM.error && <ErrorAlert error={putM.error} fallback="Failed to save credential" />}

            <DialogFooter>
              <Button type="button" variant="ghost" size="md" onClick={close} disabled={putM.isPending}>
                Cancel
              </Button>
              <Button
                type="submit"
                variant="primary"
                size="md"
                disabled={putM.isPending}
                leadingIcon={
                  putM.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <ShieldCheck className="w-4 h-4" />
                }
              >
                {putM.isPending ? 'Saving…' : isEdit ? 'Update credential' : 'Save credential'}
              </Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}
