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
 * script's FILENAME. So an injected wrapper fixed at `_mewbo_main.py` would
 * render to the user as "Mewbo Main", a platform implementation detail
 * leaking into the app's own navigation.
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
 * The `rem` basis an embedded app renders against, in px. Streamlit's ENTIRE
 * design system — heading sizes, control heights, paddings, gaps, icon boxes —
 * is authored in `rem`, and `rem` resolves against the PAGE's `<html>` element.
 * `theme.baseFontSize` does NOT reach `<html>`; it only sets `font-size` on the
 * `.stApp`/`stlite-root` wrapper, so it moves `em`-relative text and nothing
 * else. That is why an embedded app renders at full-desktop-Streamlit scale no
 * matter what `baseFontSize` says: 16px root → `st.title()` at 2.75rem = 44px,
 * 40px-tall selectboxes and tabs, 32px metric values, all inside console chrome
 * built on a 13-14px type scale. Shrinking the basis is the ONE knob that moves
 * the whole system coherently; overriding elements one at a time (an earlier
 * attempt here) fixes the handful you name and leaves everything else oversized.
 *
 * 12.5px = 78% of the 16px browser default. Chosen as the largest reduction that
 * still leaves Streamlit's own touch targets usable: it lands controls at 31px
 * against the console's 32px `Button` sizes, which is the actual goal — parity
 * with the surrounding product, not minimum size.
 */
const APP_REM_BASIS_PX = 12.5;

/** Body/label/control text, in px. Mirrors the console's `text-sm` step. */
const APP_TEXT_PX = 13;

/**
 * Vertical rhythm between sidebar elements, in px, and it must be ABSOLUTE for
 * the same reason reading text is: it is the only spacing a sidebar has left.
 *
 * facade zeroes every margin and padding it can reach
 * (`div[data-testid="stElementContainer"]`, `... > div`,
 * `div[data-testid="stVerticalBlock"] > div`) and then re-expresses ALL
 * separation as one flex `gap` — `0.125rem` on the sidebar's vertical block.
 * That is 2px against the 16px root it was authored for, already tight, and it
 * works for facade's own case: a sidebar holding a short nav list.
 *
 * Then `APP_REM_BASIS_PX` shrinks the root to 12.5px and that gap computes to
 * 1.56px. Measured on a real app's filter sidebar: a section label ("IDENTITY",
 * "LICENSING") renders as a 5px-tall box sitting 1.56px above the 55px widget
 * below it, so the label reads as colliding with the control it names, and the
 * whole panel reads as broken text rather than as a dense one. The same sidebar
 * on a page where this CSS does not run keeps Streamlit's native 8.8px and
 * looks correct — which is what made the defect look like a per-page bug rather
 * than an arithmetic one.
 *
 * 8px is Streamlit's own rhythm (0.55rem at its 16px root = 8.8px) rounded to
 * the console's 4px spacing grid. Restoring it here rather than raising the rem
 * basis keeps the density win intact: controls stay at 31px, only the space
 * BETWEEN them comes back. Two rem-derived values multiplying into an unusable
 * one is the trap — when a vendor's rem spacing has to survive a basis change,
 * pin it in px.
 */
const APP_SIDEBAR_GAP_PX = 8;

/**
 * Compact-density override, injected as a plain `st.markdown(unsafe_allow_html=True)`
 * `<style>` tag AFTER facade applies its theme.
 *
 * NOT a scale hack — no `zoom`, no `transform: scale`, both permanently banned
 * (see "Natural scale" in `apps/mewbo_console/CLAUDE.md`: fractional zoom lays
 * glyphs on fractional pixels and janks the text). Changing the `rem` basis is
 * a genuine layout recalculation: every value is re-derived at an integer-ish
 * px size and rendered crisply, which is exactly what `zoom` cannot do.
 *
 * Two layers, and both are load-bearing:
 *   1. `html { font-size }` shrinks the basis, so Streamlit's whole rem-derived
 *      system (controls, spacing, headings, metrics) comes down together.
 *   2. Absolute px sizes restore READING text — body prose, widget labels, tab
 *      labels, input values — which layer 1 would otherwise drag to ~11px.
 *      Chrome scales; text that a human reads does not.
 *
 * Measured live at 1920px against a real app: content height 9371px → 7431px
 * (-21%), h1 44→20px, metric 32→25px, selectbox/tabs 40→31px, body held at 13px.
 */
const COMPACT_DENSITY_CSS = `<style>
    html { font-size: ${APP_REM_BASIS_PX}px !important; }

    /* Reading text keeps an absolute size — see layer 2 above. .stApp is the
       inherit root for anything not named explicitly below. */
    .stApp { font-size: ${APP_TEXT_PX}px !important; }
    [data-testid="stMarkdownContainer"] p,
    [data-testid="stMarkdownContainer"] li,
    [data-testid="stWidgetLabel"] p,
    [data-testid="stMetricLabel"],
    [data-testid="stMetricLabel"] p,
    input, textarea,
    .stSelectbox div[data-baseweb="select"],
    .stMultiSelect div[data-baseweb="select"] { font-size: ${APP_TEXT_PX}px !important; }

    /* Tab labels: Streamlit styles these through a nested <p>, so the button
       itself is not the element that carries the size. */
    [data-testid="stTabs"] button,
    [data-testid="stTabs"] button p { font-size: ${APP_TEXT_PX}px !important; }

    /* Headings still need naming even after the basis shrink. Streamlit's
       defaults run 2.75rem..1rem, a full-page editorial ramp that stays
       oversized for a panel even at a 12.5px basis (2.75rem = 34px). This ramp
       is proportioned against the console's own type scale, where 20px is a
       pane title — an embedded app's title should not outrank the page's. */
    h1 { font-size: 20px !important; }
    h2 { font-size: 17px !important; }
    h3 { font-size: 15px !important; }
    h4, h5, h6 { font-size: ${APP_TEXT_PX}px !important; }

    /* A metric card's interior padding, restored to facade's OWN value.
       facade sets it on div[data-testid="stMetric"] and then defeats itself:
       its spacing reset on div[data-testid="stElementContainer"] > div carries
       one more type selector, and a metric IS that direct child, so the reset
       outranks the metric rule despite being declared earlier and the card
       computes to zero padding with label and value flush to the border. The
       reset is inside a pinned upstream wheel and cannot be narrowed at its
       source, so this re-states the intended value at the one selector where
       they collide — matching only where the reset matches. On a facade pin
       bump, re-check: fixed upstream, this becomes a harmless no-op. */
    div[data-testid="stElementContainer"] > div[data-testid="stMetric"] {
        padding: 1rem 1.25rem !important;
    }

    /* Sidebar vertical rhythm, restored in absolute px — see APP_SIDEBAR_GAP_PX
       for why facade's 0.125rem cannot survive the basis shrink. This must
       match facade's OWN selector to win on equal specificity, which it does
       because this stylesheet is injected AFTER facade's. */
    [data-testid="stSidebar"] div[data-testid="stVerticalBlock"] {
        gap: ${APP_SIDEBAR_GAP_PX}px !important;
    }
</style>`;

/**
 * `[data-testid="stSidebarNav"]` — the multipage page-link list Streamlit
 * auto-generates from a `pages/` directory — is the one sidebar element
 * `streamlit-facade`'s own CSS never targets (`facade/theme.py` styles
 * `stSidebar` and everything authored inside it, but has no rule for this
 * native chrome widget at all). Worse, unlike everything facade DOES cover,
 * this widget's color does not come from an injected `<style>` at all: it is
 * painted from Streamlit's OWN theme state at the moment it first mounts,
 * which on a cold kernel boot happens before `.streamlit/config.toml` (seeded
 * further down) is honored. The result, verified live: landing straight on
 * the app's root page shows this list in Streamlit's stock light theme (white
 * box, black text) sitting directly above the correctly dark-themed filter
 * panel below it — the one navigation step that never gets a facade rule AND
 * loses the boot-order race. Navigating to any other page forces Streamlit to
 * remount this chrome, which by then reads the correct theme, so the mismatch
 * silently self-heals — masking the defect on every page except the first one
 * a session ever lands on. A plain `st.markdown(unsafe_allow_html=True)` rule
 * sidesteps the race entirely: like `COMPACT_DENSITY_CSS`, it edits the DOM
 * directly rather than waiting on Streamlit's theme engine, so it's correct
 * from the very first paint. Colors reuse the SAME `--chrome-*` custom
 * properties facade's own `apply()` sets on `:root` (this block runs right
 * after that call, in the same wrapper), and the active/hover tints mirror
 * facade's own idiom for `stSidebar` buttons and `Sidebar()`'s active-page
 * highlight (`rgba(255,255,255,0.08)` / `0.12)`), so a themed nav list reads
 * as one system with the rest of the sidebar rather than a second palette.
 */
const SIDEBAR_NAV_CSS = `<style>
    [data-testid="stSidebarNav"] {
        background-color: var(--chrome-background) !important;
    }
    [data-testid="stSidebarNavLink"] span,
    [data-testid="stSidebarNavLink"] p {
        color: var(--chrome-foreground) !important;
        font-family: var(--font-sans) !important;
    }
    [data-testid="stSidebarNavLink"]:hover {
        background-color: rgba(255,255,255,0.08) !important;
    }
    [data-testid="stSidebarNavLink"][aria-current="page"] {
        background-color: rgba(255,255,255,0.12) !important;
        font-weight: 600 !important;
    }
    [data-testid="stSidebarNavSeparator"] {
        background-color: var(--chrome-border) !important;
    }
</style>`;

/**
 * The injected entrypoint wrapper. Applies the platform theme via
 * `streamlit-facade`, then the compact-density override above, then hands
 * control to the app's REAL entrypoint through `runpy` (so authored files stay
 * byte-pristine and tracebacks honest). The `apply(...)` call MUST live in the
 * entrypoint script — Streamlit re-executes the entrypoint on every rerun but
 * caches imported modules, so an import-once home would run `apply` exactly
 * once and, worse, facade's potential `st.rerun()` during that import would
 * permanently skip the CSS injection. ONLY `ImportError` is caught (theme is
 * cosmetic, never block the app); a `RerunException` from `apply` must
 * propagate. The density `st.markdown` call runs unconditionally — it has no
 * import to guard and no rerun of its own, so there is nothing to skip.
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
import streamlit as _mewbo_st
_mewbo_st.markdown(${JSON.stringify(COMPACT_DENSITY_CSS + SIDEBAR_NAV_CSS)}, unsafe_allow_html=True)
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
