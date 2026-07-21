/**
 * ErrorAlert — THE error banner. One shape, everywhere.
 *
 * An app-level wrapper composing the vendored shadcn `<Alert variant="destructive">`
 * (per the console's "wrappers compose, never fork" rule). It replaces three
 * competing shapes that had grown in parallel: a hand-rolled `<div>` with a
 * copy-pasted destructive class string (`SettingsView`, `PluginsPane`,
 * `ProjectsPane`, `WorktreesPanel`), a duplicated `InlineError` component
 * (`ApiKeysView`, `GitCredentialsView`), and the bare `<Alert>` in
 * `TriggersPane`.
 *
 * `error` is whatever the caller has — an `Error`, a hook's already-stringified
 * message, or `unknown` — normalized once through `getErrorMessage`.
 */
import { AlertTriangle } from 'lucide-react';

import { Alert, AlertDescription, AlertTitle } from './ui/alert';
import { getErrorMessage } from '../utils/errors';
import { cn } from '../lib/utils';

export interface ErrorAlertProps {
  error: unknown;
  /** Shown when `error` carries no message of its own. */
  fallback: string;
  /** Optional heading above the message, e.g. "Couldn't load triggers". */
  title?: string;
  className?: string;
}

export function ErrorAlert({ error, fallback, title, className }: ErrorAlertProps) {
  return (
    <Alert
      variant="destructive"
      className={cn('bg-[hsl(var(--destructive))]/10 border-[hsl(var(--destructive))]/30', className)}
    >
      <AlertTriangle className="h-4 w-4" />
      {title && <AlertTitle>{title}</AlertTitle>}
      <AlertDescription>{getErrorMessage(error, fallback)}</AlertDescription>
    </Alert>
  );
}
