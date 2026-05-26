/**
 * Runtime config injected by nginx at container start
 * (see ``docker/console-entrypoint.sh``).
 *
 * The global was renamed from ``__MEESEEKS_CONFIG__`` to
 * ``__MEWBO_CONFIG__``; production images still in rotation ship the
 * older entrypoint script, so we accept either — whichever the running
 * container's entrypoint wrote wins. New deploys produce the new name;
 * legacy ones keep working until the next image rebuild.
 *
 * Single source of truth so every consumer reads the same thing.
 */

interface RuntimeConfig {
  VITE_API_BASE_URL?: string;
  VITE_API_KEY?: string;
  VITE_API_MODE?: string;
  VITE_API_USE_PROXY?: string;
}

export function readRuntimeConfig(): RuntimeConfig | undefined {
  const win = window as unknown as Record<string, unknown>;
  return (
    (win.__MEWBO_CONFIG__ as RuntimeConfig | undefined) ??
    (win.__MEESEEKS_CONFIG__ as RuntimeConfig | undefined)
  );
}

export type { RuntimeConfig };
