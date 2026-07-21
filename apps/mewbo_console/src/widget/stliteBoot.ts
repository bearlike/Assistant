// Framework-free boot seam for stlite (Streamlit-in-WASM) widgets.
//
// This module is the single source of truth for how a `WidgetReadyPayload`
// becomes stlite kernel options. It is imported by BOTH surfaces:
//   - `StliteWidgetPanel.tsx` (the React console panel, via `useKernel`)
//   - `widget/host.ts` (the standalone `widget-host.html` page, via
//     `@stlite/browser`'s framework-free `mount()`)
//
// It MUST stay React-free (no hooks, no JSX) so the browser host can import it
// without pulling React into that bundle. The two differences between the
// surfaces — where the streamlit/stlite wheels come from (`wheelUrls`) and
// where the self-hosted Pyodide runtime lives (`pyodideUrl`) — are parameters,
// not hard-coded, precisely so this one function serves both.

import type { WidgetReadyPayload } from "../types"
import type { AppContext, AppFrontend } from "../types/apps";

export type WidgetTheme = "dark" | "light";

/** The bundled streamlit + stlite-lib wheel URLs (see @stlite `wheelUrls`). */
export interface WheelUrls {
  stliteLib: string;
  streamlit: string;
}

export interface BuildKernelOptionsInput {
  /** Console light/dark theme — flips Streamlit's bundled palette. */
  theme: WidgetTheme;
  /** Bundled streamlit + stlite-lib wheels (differs per surface/vendor). */
  wheelUrls: WheelUrls;
  /**
   * URL of the self-hosted `pyodide.mjs`. When omitted, stlite's worker falls
   * back to its jsdelivr CDN default (`cdn.jsdelivr.net/pyodide/v0.29.3/...`),
   * which is the pre-self-hosting behaviour and breaks on offline/LAN deploys.
   * The directory of this URL becomes Pyodide's `indexURL`, so the vendored
   * `pyodide-lock.json` + package wheels next to `pyodide.mjs` resolve locally.
   */
  pyodideUrl?: string;
  /**
   * stlite's `basePath` — the path Streamlit treats as the main page in a
   * multipage app, and the prefix it pushes onto history on every page switch.
   *
   * Left unset it defaults to `window.location.pathname` at kernel init, which
   * is only sensible when the page IS the app. Pass an explicit value from any
   * surface where the hosting page's pathname is not the app's own — stlite's
   * own docs recommend exactly this for hosts that serve a fallback page on
   * unknown subpaths, because otherwise a pushed `<pathname>/<Page>` URL
   * silently resolves to that fallback rather than the app.
   */
  basePath?: string;
}

// Per-mode Streamlit theme colors. The embedded kernel runs in a Pyodide worker
// that can't read the console's CSS vars, so each hex is hand-synced to
// index.css. DARK mirrors the warm-carbon dark tokens (--widget-panel-bg /
// --muted / --code-body / --foreground / --primary — the panel contract in
// CLAUDE.md "streamlitConfig dotted keys"); LIGHT is the warm_terracotta set.
export const STLITE_THEME: Record<WidgetTheme, Record<string, string>> = {
  light: {
    "theme.primaryColor":             "#d97757",
    "theme.backgroundColor":          "#faf9f5",
    "theme.secondaryBackgroundColor": "#f0efe7",
    "theme.codeBackgroundColor":      "#e8e6dc",
    "theme.textColor":                "#3d3a2a",
    "theme.linkColor":                "#d97757",
    "theme.borderColor":              "#b8b5a8",
  },
  dark: {
    "theme.primaryColor":             "#d97757",
    "theme.backgroundColor":          "#131211",
    "theme.secondaryBackgroundColor": "#2a2927",
    "theme.codeBackgroundColor":      "#0e0d0c",
    "theme.textColor":                "#faf9f5",
    "theme.linkColor":                "#d97757",
    "theme.borderColor":              "#6b6861",
  },
};

/** The `streamlit-facade` wheel pinned into every app/widget's requirements. */
export const FACADE_VERSION = "0.1.6";

// Per-mode facade theme tokens, passed to `streamlit-facade`'s
// `facade.theme.apply(...)` from the injected wrapper entrypoint. Facade
// is pure Python that stamps the platform's shadcn CSS vars onto Streamlit's
// live DOM — but the Pyodide worker can't read the console's CSS vars, so each
// value is hand-synced to index.css (SAME source as STLITE_THEME above): DARK
// mirrors the warm-carbon tokens, LIGHT the warm_terracotta set.
// `font_sans`/`font_mono` deliberately resolve to Streamlit's OWN bundled fonts
// (Source Sans Pro / Source Code Pro): facade applies `font-family:
// var(--font-sans) !important` on descendant selectors, which WOULD beat
// Streamlit's stylesheet, so these must name the fonts already in use — repo
// law is that the native Streamlit font is the deliberate embed look.
const FACADE_THEME: Record<WidgetTheme, Record<string, string>> = {
  light: {
    primary:            "#d97757",
    primary_foreground: "#ffffff",
    background:         "#faf9f5",
    foreground:         "#3d3a2a",
    muted:              "#f0efe7",
    muted_foreground:   "#888681",
    border:             "#b8b5a8",
    destructive:        "#ba3636",
    chrome_background:  "#faf9f5",
    chrome_foreground:  "#3d3a2a",
    chrome_border:      "#b8b5a8",
    font_sans:          '"Source Sans Pro", sans-serif',
    font_mono:          '"Source Code Pro", monospace',
    radius:             "6px",
  },
  dark: {
    primary:            "#d97757",
    primary_foreground: "#ffffff",
    background:         "#131211",
    foreground:         "#faf9f5",
    muted:              "#2a2927",
    muted_foreground:   "#a4a298",
    border:             "#6b6861",
    destructive:        "#e34a4a",
    chrome_background:  "#131211",
    chrome_foreground:  "#faf9f5",
    chrome_border:      "#6b6861",
    font_sans:          '"Source Sans Pro", sans-serif',
    font_mono:          '"Source Code Pro", monospace',
    radius:             "6px",
  },
};

/**
 * Streamlit derives the multipage sidebar's MAIN-PAGE label from the entrypoint
 * script's FILENAME. So an injected wrapper named `_mewbo_main.py` — which is
 * what the entrypoint used to be — rendered to the user as "Mewbo Main", a
 * platform implementation detail leaking into the app's own navigation.
 *
 * The fix keeps every existing law and only moves names around: the wrapper
 * takes over the AUTHORED entrypoint's path (so the label is whatever the app
 * author called it — `home.py` reads as "Home"), and the authored source is
 * relocated verbatim to a private sibling that `runpy` targets.
 *
 * Sibling, not a fixed top-level name, because the relocated file must keep the
 * authored entrypoint's DIRECTORY: sibling-module imports resolve relative to
 * it, so moving `pages/home.py` to a root-level private name would break them.
 *
 * Traceback honesty, precisely: the authored bytes are copied unmodified, so
 * every line NUMBER in a traceback still points at the right line, and the
 * private name is a mechanical `_mewbo_app_` prefix on the original — the one
 * thing that changes is the displayed filename, which stays trivially
 * mappable back to the file the author wrote.
 */
function relocatedEntrypoint(entrypoint: string): string {
  const slash = entrypoint.lastIndexOf("/");
  const dir = entrypoint.slice(0, slash + 1); // "" when there is no slash
  return `${dir}_mewbo_app_${entrypoint.slice(slash + 1)}`;
}

/**
 * The injected entrypoint wrapper. Applies the platform theme via
 * `streamlit-facade`, then hands control to the app's REAL entrypoint through
 * `runpy` (so authored files stay byte-pristine and tracebacks honest). The
 * `apply(...)` call MUST live in the entrypoint script — Streamlit re-executes
 * the entrypoint on every rerun but caches imported modules, so an import-once
 * home would run `apply` exactly once and, worse, facade's potential
 * `st.rerun()` during that import would permanently skip the CSS injection.
 * ONLY `ImportError` is caught (theme is cosmetic, never block the app); a
 * `RerunException` from `apply` must propagate.
 */
function facadeMainPy(theme: WidgetTheme, entrypoint: string): string {
  const t = FACADE_THEME[theme];
  return `# Injected by Mewbo (stliteBoot) — applies the platform theme, then runs the
# app's real entrypoint. Not part of the app's authored sources.
try:
    import facade.theme as _mewbo_facade_theme
except ImportError:  # theme is cosmetic — never block the app on it
    _mewbo_facade_theme = None
if _mewbo_facade_theme is not None:
    _mewbo_facade_theme.apply(
        base=${JSON.stringify(theme)},
        primary=${JSON.stringify(t.primary)},
        primary_foreground=${JSON.stringify(t.primary_foreground)},
        background=${JSON.stringify(t.background)},
        foreground=${JSON.stringify(t.foreground)},
        muted=${JSON.stringify(t.muted)},
        muted_foreground=${JSON.stringify(t.muted_foreground)},
        border=${JSON.stringify(t.border)},
        destructive=${JSON.stringify(t.destructive)},
        chrome_background=${JSON.stringify(t.chrome_background)},
        chrome_foreground=${JSON.stringify(t.chrome_foreground)},
        chrome_border=${JSON.stringify(t.chrome_border)},
        font_sans='"Source Sans Pro", sans-serif',
        font_mono='"Source Code Pro", monospace',
        radius=${JSON.stringify(t.radius)},
    )
import runpy
runpy.run_path(${JSON.stringify(entrypoint)}, run_name="__main__")
`;
}

/**
 * `.streamlit/config.toml`, pre-seeded BYTE-IDENTICAL to what facade 0.1.6's
 * `_write_config` emits. `facade.theme.apply()` writes this file and calls
 * `st.rerun()` whenever the on-disk content differs from what it would write —
 * seeding the exact bytes here means the first boot finds them already correct
 * and skips that extra rerun.
 */
function facadeConfigToml(theme: WidgetTheme): string {
  const t = FACADE_THEME[theme];
  return (
    `[theme]\n` +
    `base = "${theme}"\n` +
    `primaryColor = "${t.primary}"\n` +
    `backgroundColor = "${t.background}"\n` +
    `secondaryBackgroundColor = "${t.muted}"\n` +
    `textColor = "${t.foreground}"\n`
  );
}

/** A normalized frontend bundle — the common shape a widget AND an app reduce
 *  to before becoming kernel options. */
interface NormalizedFrontend {
  entrypoint: string;
  files: Record<string, string>;
  requirements: string[];
}

/**
 * The single kernel-option builder both surfaces funnel through. Files are
 * passed INLINE via the `files` option (no CORS, no server fetch);
 * `requirements` install via micropip inside Pyodide. `streamlitConfig` dotted
 * keys mirror `.streamlit/config.toml` — `client.toolbarMode: "viewer"` +
 * `ui.hideTopBar` strip Streamlit's chrome (Deploy/hamburger band + the
 * "Running…" status widget) config-natively, and `theme.base` flips its
 * bundled stylesheet, while STLITE_THEME overrides the palette to match the
 * console's CSS variables.
 *
 * ALL surfaces additionally get the platform shadcn theme with ZERO changes to
 * authored sources: a `streamlit-facade` requirement, a wrapper that takes over
 * the authored entrypoint's PATH (it applies the theme, then `runpy`s the
 * app's real entrypoint, relocated verbatim to a private sibling — see
 * `relocatedEntrypoint` for why the wrapper wears the authored name), and a
 * pre-seeded `.streamlit/config.toml`. Every injected file wins over a
 * same-named authored file, the same precedent as `_app_context.json`.
 */
function kernelOptionsFor(
  { entrypoint, files, requirements }: NormalizedFrontend,
  { theme, wheelUrls, pyodideUrl, basePath }: BuildKernelOptionsInput,
) {
  // Never mutate the caller's requirements — build a NEW array. Skip the append
  // if the app already declares any streamlit-facade requirement (its pin wins).
  const withFacade = requirements.some((r) => /^streamlit-facade\b/.test(r))
    ? requirements
    : [...requirements, `streamlit-facade==${FACADE_VERSION}`];

  // The authored entrypoint's source moves to a private sibling; the wrapper
  // takes over its path so Streamlit's main-page label reads as the author's
  // own filename (see `relocatedEntrypoint`). Both injected files win over
  // same-named authored files — spread first, then override — which is the
  // same precedent as `_app_context.json`: an app cannot smuggle its own
  // wrapper in by pre-declaring either name.
  const runPath = relocatedEntrypoint(entrypoint);
  const authored = files[entrypoint];
  const withTheme: Record<string, string> = {
    ...files,
    // Relocate ONLY a file that exists. Seeding an empty placeholder for a
    // missing entrypoint would make `runpy` succeed on nothing — a blank app
    // and no error anywhere, where the pre-wrapper behaviour surfaced a
    // FileNotFoundError naming the path. Every caller already guarantees the
    // key is present (`AppFrontend._entrypoint_present` server-side,
    // `parseAppMessage`/`parseWidgetMessage` on the host bus), so this branch
    // should be unreachable — it exists to keep the failure LOUD if it isn't.
    ...(authored !== undefined ? { [runPath]: authored } : {}),
    [entrypoint]: facadeMainPy(theme, runPath),
    ".streamlit/config.toml": facadeConfigToml(theme),
  };

  return {
    entrypoint,
    files: Object.fromEntries(
      Object.entries(withTheme).map(([name, content]) => [name, { data: content }]),
    ),
    requirements: withFacade,
    prebuiltPackageNames: [] as string[],
    archives: [] as never[],
    wheelUrls,
    // Only set when self-hosted; leaving it undefined preserves stlite's CDN
    // default so a mis-vendored build degrades to "works online" rather than
    // "fails to boot".
    ...(pyodideUrl ? { pyodideUrl } : {}),
    // Likewise conditional: absent, stlite keeps its `window.location.pathname`
    // default, which is the correct reading for an in-document mount where the
    // console's own URL is the app's URL.
    ...(basePath != null ? { basePath } : {}),
    streamlitConfig: {
      "client.toolbarMode": "viewer",
      "ui.hideTopBar": true,
      "theme.base": theme,
      "theme.font": "sans serif",
      "theme.showWidgetBorder": true,
      "theme.baseRadius": "6px",
      "theme.baseFontSize": 14,
      ...STLITE_THEME[theme],
    },
  };
}

/**
 * Turn a `WidgetReadyPayload` into stlite kernel options. The widget's `app.py`
 * and `data.json` are the fixed two-file bundle; the authored entrypoint is
 * `app.py`, which `kernelOptionsFor` relocates to `_mewbo_app_app.py` and
 * fronts with the injected wrapper at `app.py`.
 * The widget path deliberately gains that injected theme wrapper too — both
 * surfaces stay byte-identical to EACH OTHER through the shared
 * `kernelOptionsFor`.
 */
export function buildKernelOptions(
  payload: WidgetReadyPayload,
  options: BuildKernelOptionsInput,
) {
  return kernelOptionsFor(
    { entrypoint: "app.py", files: payload.files, requirements: payload.requirements },
    options,
  );
}

/**
 * Turn a multi-file `AppFrontend` + its render `AppContext` into stlite kernel
 * options. Same boot/theme path as widgets, generalized two ways: the file map
 * is arbitrary (path → source) with a caller-declared `entrypoint`, and the
 * render context is injected as an extra `_app_context.json` file so the app's
 * bundled Python SDK reads its token + API base from disk (no network fetch to
 * bootstrap, no secret baked into the generated code). A frontend that already
 * names `_app_context.json` cannot smuggle its own — the injected value wins.
 */
export function buildAppKernelOptions(
  frontend: AppFrontend,
  appContext: AppContext,
  options: BuildKernelOptionsInput,
) {
  return kernelOptionsFor(
    {
      entrypoint: frontend.entrypoint,
      files: { ...frontend.files, "_app_context.json": JSON.stringify(appContext) },
      requirements: frontend.requirements,
    },
    options,
  );
}
