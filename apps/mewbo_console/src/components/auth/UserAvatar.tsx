/**
 * `UserAvatar` — the one avatar renderer, wherever a principal is shown.
 *
 * Composes the vendored Radix `ui/avatar` primitive (per the console's
 * "wrappers compose, never fork" rule) and adds the one thing the primitive
 * does not have: a multi-candidate source chain.
 *
 * Radix's `<AvatarImage>` falls back to `<AvatarFallback>` on a load error, but
 * only across ONE source. An identity provider's `picture_url` that 404s or is
 * blocked by a referrer policy should drop to Gravatar before it drops to
 * initials, so this walks the chain itself: each failure advances an index, and
 * only exhausting the list reveals the initials. That is why the component
 * holds state at all.
 */
import { useEffect, useState } from "react";

import { cn } from "@/lib/utils";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";

export interface UserAvatarProps {
  /**
   * Image candidates, highest precedence first — normally
   * `AuthSession.avatarSources` (`avatar.picture_url` then
   * `avatar.gravatar_url`). Falsy entries are ignored.
   */
  sources?: readonly (string | null | undefined)[];
  /** Letters shown once every source has failed or none was supplied. */
  initials: string;
  /** Accessible name; also the image `alt`. */
  name?: string;
  /** Tailwind size classes for the root. Defaults to a 24px rail-sized dot. */
  className?: string;
}

export function UserAvatar({ sources, initials, name, className }: UserAvatarProps) {
  const candidates = (sources ?? []).filter((src): src is string => Boolean(src));
  const [index, setIndex] = useState(0);

  // A different principal (sign-in, account switch) must restart the chain,
  // otherwise a stale "everything failed" index hides the new user's picture.
  const chainKey = candidates.join("|");
  useEffect(() => {
    setIndex(0);
  }, [chainKey]);

  const current = candidates[index];

  return (
    <Avatar className={cn("size-6", className)}>
      {current && (
        <AvatarImage
          // Remount on every candidate so Radix re-runs its load for each one.
          key={current}
          src={current}
          alt={name ?? ""}
          onError={() => setIndex((i) => i + 1)}
        />
      )}
      <AvatarFallback
        className="bg-[hsl(var(--primary))] text-2xs font-medium text-[hsl(var(--primary-foreground))]"
        // Initials are decorative when a name is already rendered beside them;
        // the accessible name comes from the labelled control that wraps this.
        aria-hidden={Boolean(name)}
      >
        {initials}
      </AvatarFallback>
    </Avatar>
  );
}
