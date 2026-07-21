/**
 * SecretsSummary — read-only "which secrets are configured" pane (Security).
 *
 * A pane in the facet registry (`../panes.ts`), so it takes ZERO props and
 * fetches its own data: `useConfig()` reads the SAME TanStack cache entry the
 * shell already populated (shared by queryKey — calling the hook again is not a
 * second fetch), which is what lets the registry stay a plain data map instead
 * of a switch that threads bespoke props per facet.
 *
 * The backend strips secret VALUES, so "configured" can only ever come from the
 * `secrets` is-set map — never from a value in `config`.
 */
import { Check, Minus } from "lucide-react";

import { cn } from "../../../lib/utils";
import { useConfig } from "../../../hooks/useConfig";
import { SettingsCard } from "../SettingsCard";
import { cardSurface } from "../../ui/card-surface";

const SECRETS_HELP = [
  "The secrets Mewbo uses to reach other services: provider keys, tokens, and passwords.",
  "",
  "A stored secret is never sent back to the console, so this list can only tell you whether each one is **set**. To change one, open the section that owns it: the field offers **Replace** once a value exists.",
  "",
  "These are the secrets Mewbo *uses*. The keys it *hands out* to callers are below.",
].join("\n");

export function SecretsSummary() {
  const { secrets } = useConfig();
  const paths = Object.keys(secrets).sort();

  return (
    <SettingsCard
      id="settings-secrets-summary"
      title="Configured secrets"
      description={SECRETS_HELP}
    >
      {paths.length === 0 ? (
        <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            No secrets are set yet.
          </p>
          <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
            Every field Mewbo treats as a secret, such as the LLM provider key, the tracing
            keys, and channel tokens, appears here once you save a value for it.
          </p>
        </div>
      ) : (
        <ul className="space-y-1.5">
          {paths.map((path) => (
            <li
              key={path}
              className={cn(cardSurface({ radius: "right" }), "flex items-center justify-between gap-3 px-3 py-2")}
            >
              <code className="text-xs font-mono text-[hsl(var(--foreground))] break-all">
                {path}
              </code>
              {secrets[path] ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-[hsl(var(--success))]/15 px-2 py-0.5 text-2xs font-medium leading-none text-[hsl(var(--success))] border border-[hsl(var(--success))]/20">
                  <Check className="w-3 h-3" /> set
                </span>
              ) : (
                <span className="inline-flex items-center gap-1 rounded-full bg-[hsl(var(--muted))]/40 px-2 py-0.5 text-2xs font-medium leading-none text-[hsl(var(--muted-foreground))] border border-[hsl(var(--border))]">
                  <Minus className="w-3 h-3" /> not set
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </SettingsCard>
  );
}
