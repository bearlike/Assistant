// Shared stlite-panel chrome — the theme/title/scrollbar plumbing that an
// IN-DOCUMENT stlite mount needs. Its one remaining full consumer is
// `StliteWidgetPanel` (the chat `widget_ready` card), which still mounts
// Streamlit into the console's own DOM.
//
// Mewbo Apps no longer render this way: `apps/AppFrame.tsx` frames the
// standalone `widget-host.html` page instead, and takes only `useConsoleTheme`
// from here. Everything else in this module exists to contain a Streamlit
// mount that shares the console's document — the title hijack, the injected
// `<style>` bleed, the Streamlit-DOM overrides — and a framed app cannot cause
// any of it. Read that as the direction of travel: this module shrinks as
// surfaces move behind the frame, and nothing new should be added here that a
// framed mount would also need.
//
// The framework-free `widget-host.html` path stays on `widget/stliteBoot.ts`
// (React-free) — this module is the React-side counterpart and may use hooks.
//
// Font injection (`useFontInjection`) was removed in a later change: Streamlit sets an
// explicit `font-family` on `p`/headings/captions, and an ancestor `!important`
// rule cannot beat a descendant's own explicit rule, so the injected Inter
// override was inert for visible text — it only mis-fonted code chips to
// JetBrains Mono. Native Source Sans is the deliberate embed look. Don't
// reintroduce a font-override hook here.

import { useEffect, useState, type RefObject } from "react";

export type StliteTheme = "dark" | "light";

// Self-hosted Pyodide runtime. Vite serves `public/pyodide/` at
// `${BASE_URL}pyodide/`; passing this as `pyodideUrl` keeps the whole core boot
// (pyodide runtime + micropip + numpy/pandas + the stlite wheels) on our own
// origin instead of stlite's jsdelivr CDN default — which is what makes stlite
// boot on an offline / LAN-only deploy. See `widget/stliteBoot.ts` and
// `scripts/fetch-pyodide.mjs`.
export const PYODIDE_URL = `${import.meta.env.BASE_URL}pyodide/pyodide.mjs`;

/**
 * The Streamlit-DOM override classes that every in-console stlite mount needs.
 * Kept as one shared constant so every in-document Streamlit mount agrees about
 * which Streamlit chrome to nuke. Each entry is load-bearing (see
 * `apps/mewbo_console/CLAUDE.md` → "Stlite widget rendering"):
 *
 *   - `stHeader/stToolbar/stDecoration` hidden: the Running indicator + top
 *     gradient bar `toolbarMode:"viewer"` leaves behind, which overlap content.
 *   - `.stMainBlockContainer` un-capped + zero side padding: Streamlit's ~736px
 *     `layout="centered"` cap otherwise leaves the panel half-empty.
 *   - `stMain` overflow visible + `stAppViewContainer` the sole scroller: kills
 *     the double nested scrollbar (users scroll the inner one and never reach
 *     the outer's content).
 */
export const STLITE_STREAMLIT_OVERRIDES = [
  "[&_[data-testid='stHeader']]:!hidden",
  "[&_[data-testid='stToolbar']]:!hidden",
  "[&_[data-testid='stDecoration']]:!hidden",
  "[&_.stMainBlockContainer]:!max-w-none",
  "[&_.stMainBlockContainer]:!w-full",
  "[&_.stMainBlockContainer]:!px-3",
  "[&_.stMainBlockContainer]:!pt-3",
  "[&_.stMainBlockContainer]:!pb-3",
  "[&_[data-testid='stMain']]:!overflow-visible",
  "[&_.stAppViewContainer]:!overflow-y-scroll",
].join(" ");

/**
 * Keep stlite from hijacking the browser tab title. When an app's `app.py`
 * doesn't call `st.set_page_config(page_title=...)`, Streamlit forces
 * `document.title = "Streamlit"` during Pyodide boot. We observe `<title>` and
 * revert to the last non-"Streamlit" value — which lets legitimate App.tsx
 * updates (navigation, renames) flow through while pinning stlite's default out.
 */
export function useTitleGuard() {
  useEffect(() => {
    const titleEl = document.querySelector("title");
    if (!titleEl) return;
    let lastGood = document.title;
    const obs = new MutationObserver(() => {
      if (document.title === "Streamlit") {
        document.title = lastGood;
      } else {
        lastGood = document.title;
      }
    });
    obs.observe(titleEl, { childList: true });
    return () => obs.disconnect();
  }, []);
}

/**
 * Subscribe to the Mewbo console's theme. App.tsx toggles by adding/removing
 * the `light` class on `<html>` (dark is the default, with no class); a
 * MutationObserver on that single attribute is cheaper than a React context +
 * provider and fires exactly once per toggle.
 */
export function useConsoleTheme(): StliteTheme {
  const read = (): StliteTheme =>
    document.documentElement.classList.contains("light") ? "light" : "dark";
  const [theme, setTheme] = useState<StliteTheme>(read);
  useEffect(() => {
    const obs = new MutationObserver(() => setTheme(read()));
    obs.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => obs.disconnect();
  }, []);
  return theme;
}

// ── Style-scope guard ────────────────────────────────────────────────────────
//
// stlite mounts Streamlit into the HOST document DOM (no iframe). The injected
// `streamlit-facade` theming emits a `<style>` tag (via `st.markdown(...,
// unsafe_allow_html=True)`) whose rules were written for a real Streamlit page
// that OWNS the document: `body { background-color: … !important }`, `:root`
// token vars, broad `font-family: var(--font-sans) !important`. Unscoped, those
// globals repaint the WHOLE console when the widget renders inside a React
// panel. The same hazard applies to any `<style>` a widget author injects.
//
// The guard rewrites every such `<style>` so its rules can only match inside the
// panel: prefix normal selectors with the scope, and REPLACE a leading
// `body`/`html`/`:root` head token with the scope (so a page-owning rule becomes
// a container-owning one). The law: styles born inside a widget stay inside the
// widget. The standalone `widget-host.html` page is deliberately NOT guarded —
// there the page IS the widget, so global rules are correct.

/** Class stamped on the panel container; the scope selector is `.${…}`. */
export const STLITE_SCOPE_CLASS = "mewbo-stlite-scope";

/** Marks a `<style>` the guard has already rewritten, so re-observation skips it. */
const SCOPED_MARK = "mewboScoped"; // → data-mewbo-scoped

// A selector's head token is `body`/`html`/`:root` only at a real token
// boundary: `body`, `body.foo`, `body >` match; `bodyguard` must not. The
// negative lookahead for a name char is what enforces that boundary.
const HEAD_TOKEN = /^(html|body|:root)(?![\w-])/;

/**
 * Confine one selector to the scope. If its FIRST compound is a page-owning head
 * token (`body`/`html`/`:root`), replace that token with the scope; otherwise
 * prefix the whole selector with the scope.
 */
function scopeOneSelector(selector: string, scope: string): string {
  const trimmed = selector.trim();
  if (!trimmed) return trimmed;
  if (HEAD_TOKEN.test(trimmed)) return trimmed.replace(HEAD_TOKEN, scope);
  return `${scope} ${trimmed}`;
}

/**
 * Split a `selectorText` on TOP-LEVEL commas and scope each selector.
 *
 * Limitation: this is a naive comma split — it does NOT skip commas nested in
 * `[attr="a,b"]`, `:is(a, b)`, or `color-mix(…, …)`. That is acceptable for
 * facade 0.1.6, whose selectors contain no commas inside brackets/functions;
 * only whole-selector-list commas appear. Revisit if the facade grows nested
 * comma selectors.
 */
function scopeSelectorText(selectorText: string, scope: string): string {
  return selectorText
    .split(",")
    .map((s) => scopeOneSelector(s, scope))
    .join(", ");
}

/** Re-emit a list of CSS rules, scoping each. Used at sheet root and inside groups. */
function scopeRuleList(rules: CSSRuleList, scope: string): string {
  const out: string[] = [];
  for (let i = 0; i < rules.length; i++) {
    out.push(scopeRule(rules[i], scope));
  }
  return out.join("\n");
}

/**
 * Scope a single CSS rule:
 *   - style rule → swap its selector for the scoped one, declarations verbatim
 *     (kept byte-faithful — including `!important` — by slicing from the first
 *     `{` of the rule's own `cssText`).
 *   - `@media` / `@supports` grouping rule → recurse into its `cssRules` and
 *     re-emit wrapped in the original condition prelude.
 *   - `@keyframes` / `@font-face` / anything else → emit verbatim (unscopable).
 *
 * The rule-type checks are `typeof`-guarded because jsdom's CSSOM is limited: it
 * has `CSSStyleRule`/`CSSMediaRule` but NOT `CSSSupportsRule`, and a bare
 * reference to an absent global throws — so we probe with `typeof` before using
 * the constructor in `instanceof`. Any rule that still throws while being
 * rewritten is emitted verbatim rather than dropped or allowed to abort the
 * whole sheet.
 */
function scopeRule(rule: CSSRule, scope: string): string {
  try {
    const styleRule = rule as CSSStyleRule;
    const isStyleRule =
      typeof CSSStyleRule !== "undefined"
        ? rule instanceof CSSStyleRule
        : typeof styleRule.selectorText === "string" && "style" in rule;
    if (isStyleRule && typeof styleRule.selectorText === "string") {
      const scoped = scopeSelectorText(styleRule.selectorText, scope);
      const cssText = rule.cssText;
      const brace = cssText.indexOf("{");
      const declBlock = brace >= 0 ? cssText.slice(brace) : "{}";
      return `${scoped} ${declBlock}`;
    }

    const groupRule = rule as CSSGroupingRule;
    const isGroupRule =
      (typeof CSSMediaRule !== "undefined" && rule instanceof CSSMediaRule) ||
      (typeof CSSSupportsRule !== "undefined" && rule instanceof CSSSupportsRule);
    if (isGroupRule && groupRule.cssRules) {
      // The prelude ("@media …" / "@supports …") is everything before the block.
      const brace = rule.cssText.indexOf("{");
      const prelude = brace >= 0 ? rule.cssText.slice(0, brace).trimEnd() : "@media all";
      const inner = scopeRuleList(groupRule.cssRules, scope);
      return `${prelude} {\n${inner}\n}`;
    }

    return rule.cssText;
  } catch {
    // Defensive: a rule type the CSSOM can't cleanly round-trip survives as-is.
    return rule.cssText ?? "";
  }
}

/**
 * Rewrite one `<style>` element's rules to be scope-confined under `scopeSelector`
 * (e.g. `.mewbo-stlite-scope`). Parses via the element's CSSOM
 * (`styleEl.sheet.cssRules`), never regex over raw text, then assigns the rebuilt
 * text once. The element is marked `data-mewbo-scoped` BEFORE the reassignment so
 * the MutationObserver's childList record for the replaced text node finds it
 * already-scoped and skips it (idempotent re-observation). A just-inserted
 * element whose `sheet` is not yet attached is retried on the next microtask.
 */
export function scopeStyleElement(styleEl: HTMLStyleElement, scopeSelector: string): void {
  if (styleEl.dataset[SCOPED_MARK]) return;
  const sheet = styleEl.sheet;
  if (!sheet) {
    // CSSOM not attached yet (rare — browsers populate `.sheet` synchronously on
    // insertion). Retry once the microtask queue drains; bail if it detached.
    if (styleEl.isConnected) queueMicrotask(() => scopeStyleElement(styleEl, scopeSelector));
    return;
  }
  const rebuilt = scopeRuleList(sheet.cssRules, scopeSelector);
  // Mark first, then reassign — see the docstring on ordering.
  styleEl.dataset[SCOPED_MARK] = "true";
  styleEl.textContent = rebuilt;
}

/**
 * Confine every `<style>` born inside `ref`'s subtree to the panel. Stamps the
 * `mewbo-stlite-scope` class on the container, scopes any `<style>` already
 * present, then observes (childList + subtree) and scopes every `<style>` that
 * later appears — facade re-injects its theming `<style>` on every Streamlit
 * rerun. Already-marked elements are skipped. Disconnects on cleanup.
 *
 * Attach the ref to the element that CONTAINS the stlite mount. For
 * `StliteWidgetPanel` that is the PORTALLED wrapper hosting `StliteAppWithToast`
 * (the panel is portalled to `document.body` by `WidgetCard`), not the inline
 * placeholder.
 */
export function useStliteStyleScope(ref: RefObject<HTMLElement | null>): void {
  useEffect(() => {
    const root = ref.current;
    if (!root) return;
    root.classList.add(STLITE_SCOPE_CLASS);
    const scope = `.${STLITE_SCOPE_CLASS}`;

    const scopeIfStyle = (node: Node) => {
      if (node instanceof HTMLStyleElement) {
        if (!node.dataset[SCOPED_MARK]) scopeStyleElement(node, scope);
      } else if (node instanceof Element) {
        // A subtree was inserted that may CONTAIN <style> descendants.
        node.querySelectorAll("style").forEach((el) => {
          if (!el.dataset[SCOPED_MARK]) scopeStyleElement(el, scope);
        });
      }
    };

    // <style>s already in the subtree at mount.
    root.querySelectorAll("style").forEach((el) => {
      if (!el.dataset[SCOPED_MARK]) scopeStyleElement(el, scope);
    });

    const obs = new MutationObserver((records) => {
      for (const rec of records) {
        rec.addedNodes.forEach(scopeIfStyle);
      }
    });
    obs.observe(root, { childList: true, subtree: true });
    return () => obs.disconnect();
  }, [ref]);
}
