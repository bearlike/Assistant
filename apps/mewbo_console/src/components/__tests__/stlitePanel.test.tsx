/**
 * Style-scope guard (`stlitePanel.ts`) — the fix for the streamlit-facade follow-up.
 *
 * stlite mounts Streamlit into the HOST document (no iframe), and the injected
 * `streamlit-facade` theming emits a `<style>` written for a real Streamlit page
 * that OWNS the document (`body { … !important }`, `:root` token vars, broad
 * `font-family: var(--font-sans) !important`). Unscoped, those globals repaint
 * the WHOLE console when a widget renders inside a React panel. `scopeStyleElement`
 * rewrites such a `<style>` so its rules can only match inside the panel, and
 * `useStliteStyleScope` keeps doing it as facade re-injects on every rerun.
 *
 * The fixture below is a REAL slice of `streamlit-facade` 0.1.6's `theme.apply()`
 * CSS (its `theme.py` big f-string, ~line 300 onward) — the `:root` token block,
 * the `header[data-testid="stHeader"]` rule, the `.stApp, body` background rule,
 * and the `html, body { font-family: var(--font-sans) !important }` rule. Two
 * synthetic rules (`@media` + `@keyframes`) are appended, clearly marked, to
 * exercise the grouping-recurse and verbatim-passthrough branches, which facade
 * 0.1.6 itself does not have.
 */
import { cleanup, render, waitFor } from "@testing-library/react";
import { useRef } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { scopeStyleElement, STLITE_SCOPE_CLASS, useStliteStyleScope } from "../stlitePanel";

const SCOPE = `.${STLITE_SCOPE_CLASS}`;

// ── REAL slice of streamlit-facade 0.1.6's theme.apply() CSS ─────────────────
const FACADE_CSS = `
  :root {
    --primary: #d97757;
    --background: #131211;
    --foreground: #faf9f5;
    --font-sans: system-ui;
  }
  header[data-testid="stHeader"] {
    background-color: var(--background) !important;
    border-bottom: none !important;
    box-shadow: none !important;
  }
  .stApp, body {
    background-color: var(--background) !important;
  }
  html, body {
    font-family: var(--font-sans) !important;
  }
`;

// ── Synthetic tail: NOT from facade 0.1.6; here to cover the grouping-recurse
//    and the verbatim (@keyframes) branches. ──────────────────────────────────
const SYNTHETIC_TAIL = `
  @media (max-width: 600px) {
    body { color: red; }
    .stApp { color: blue; }
  }
  @keyframes stspin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }
`;

/** Append a `<style>` to the document and return it (its `.sheet` is populated). */
function mountStyle(css: string): HTMLStyleElement {
  const el = document.createElement("style");
  el.textContent = css;
  document.head.appendChild(el);
  return el;
}

/** Read back the scoped selectors, in document order (skips at-rules w/o selectorText). */
function scopedSelectors(el: HTMLStyleElement): string[] {
  const out: string[] = [];
  const rules = el.sheet?.cssRules;
  if (!rules) return out;
  for (let i = 0; i < rules.length; i++) {
    const s = (rules[i] as CSSStyleRule).selectorText;
    if (typeof s === "string") out.push(s);
  }
  return out;
}

afterEach(() => {
  cleanup();
  document.querySelectorAll("style").forEach((el) => el.remove());
});

describe("scopeStyleElement", () => {
  it("REPLACES a `body`/`html`/`:root` head token with the scope (never prefixes it)", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);
    const selectors = scopedSelectors(el);

    // `:root` → the scope itself (not `.scope :root`).
    expect(selectors).toContain(SCOPE);
    // `html, body` → both head tokens collapse onto the scope.
    expect(selectors).toContain(`${SCOPE}, ${SCOPE}`);
    // Nowhere did a head token get prefixed instead of replaced.
    expect(el.textContent).not.toContain(`${SCOPE} body`);
    expect(el.textContent).not.toContain(`${SCOPE} html`);
    expect(el.textContent).not.toContain(`${SCOPE} :root`);
  });

  it("PREFIXES a normal selector with the scope", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);
    const selectors = scopedSelectors(el);
    // The real facade header rule is a plain descendant target → prefixed.
    expect(selectors).toContain(`${SCOPE} header[data-testid="stHeader"]`);
  });

  it("handles a comma list per-selector (.stApp prefixed, body replaced, in ONE list)", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);
    // `.stApp, body` → `.scope .stApp, .scope` — prefix + head-replace side by side.
    expect(scopedSelectors(el)).toContain(`${SCOPE} .stApp, ${SCOPE}`);
  });

  it("does NOT treat a `bodyguard`-style token as the head token (boundary respected)", () => {
    const el = mountStyle(`.bodyish { color: green; } bodyguard-thing { color: teal; }`);
    scopeStyleElement(el, SCOPE);
    const selectors = scopedSelectors(el);
    // Both are ordinary selectors: prefixed, not head-replaced.
    expect(selectors).toContain(`${SCOPE} .bodyish`);
    expect(selectors).toContain(`${SCOPE} bodyguard-thing`);
  });

  it("keeps the declaration block byte-faithful, including `!important`", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);
    // The load-bearing global rule facade ships — the declaration must survive.
    expect(el.textContent).toContain("font-family: var(--font-sans) !important");
    expect(el.textContent).toContain("background-color: var(--background) !important");
  });

  it("recurses into an @media group, scoping its inner rules under the original condition", () => {
    const el = mountStyle(FACADE_CSS + SYNTHETIC_TAIL);
    scopeStyleElement(el, SCOPE);
    const text = el.textContent ?? "";
    // The @media prelude survives, and its inner `body`/`.stApp` rules are scoped.
    expect(text).toContain("@media (max-width: 600px)");
    const mediaRule = Array.from(el.sheet?.cssRules ?? []).find(
      (r) => r instanceof CSSMediaRule,
    ) as CSSMediaRule | undefined;
    expect(mediaRule).toBeTruthy();
    const inner = Array.from(mediaRule?.cssRules ?? []).map((r) => (r as CSSStyleRule).selectorText);
    expect(inner).toContain(SCOPE); // inner `body` → scope
    expect(inner).toContain(`${SCOPE} .stApp`); // inner `.stApp` → prefixed
  });

  it("emits a non-style at-rule (@keyframes) verbatim (unscopable)", () => {
    const el = mountStyle(FACADE_CSS + SYNTHETIC_TAIL);
    scopeStyleElement(el, SCOPE);
    const text = el.textContent ?? "";
    // Survives, and is NOT scope-prefixed (a keyframe name is not a selector).
    expect(text).toContain("@keyframes stspin");
    expect(text).not.toContain(`${SCOPE} @keyframes`);
  });

  it("is idempotent: a marked element is skipped on the second pass", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);
    expect(el.dataset.mewboScoped).toBe("true");
    const afterFirst = el.textContent;

    scopeStyleElement(el, SCOPE); // second pass — must be a no-op
    expect(el.textContent).toBe(afterFirst);
    // Double-scoping would have produced `.scope .scope .stApp`; it must not.
    expect(el.textContent).not.toContain(`${SCOPE} ${SCOPE}`);
  });

  it("THE BUG: after scoping, a rule can no longer match an element OUTSIDE the container", () => {
    const el = mountStyle(FACADE_CSS);
    scopeStyleElement(el, SCOPE);

    // Build a container (scoped) with an inner .stApp, plus an OUTSIDE .stApp.
    const container = document.createElement("div");
    container.className = STLITE_SCOPE_CLASS;
    const inside = document.createElement("div");
    inside.className = "stApp";
    container.appendChild(inside);
    const outside = document.createElement("div");
    outside.className = "stApp";
    document.body.append(container, outside);

    // `.stApp, body` was rewritten to `.scope .stApp, .scope`, so the scoped
    // `.stApp` compound matches the inside node and NOT the outside one.
    expect(scopedSelectors(el)).toContain(`${SCOPE} .stApp, ${SCOPE}`);
    const stAppSelector = `${SCOPE} .stApp`;
    expect(inside.matches(stAppSelector)).toBe(true);
    expect(outside.matches(stAppSelector)).toBe(false);

    // And the former `body { … }` rule now targets the container, never <body>.
    expect(document.body.matches(SCOPE)).toBe(false);
    expect(container.matches(SCOPE)).toBe(true);

    container.remove();
    outside.remove();
  });

  it("defensively survives a document with no matching container (guard never throws)", () => {
    // The whole point of the guard: rules are inert until a `.scope` exists.
    const el = mountStyle(FACADE_CSS);
    expect(() => scopeStyleElement(el, SCOPE)).not.toThrow();
    expect(document.body.matches(SCOPE)).toBe(false);
  });
});

describe("useStliteStyleScope", () => {
  function Harness() {
    const ref = useRef<HTMLDivElement>(null);
    useStliteStyleScope(ref);
    return <div ref={ref} data-testid="container" />;
  }

  it("stamps the scope class on the container and scopes a <style> injected AFTER mount", async () => {
    const { getByTestId } = render(<Harness />);
    const container = getByTestId("container");
    expect(container.classList.contains(STLITE_SCOPE_CLASS)).toBe(true);

    // facade re-injects its <style> on every Streamlit rerun — simulate one.
    const style = document.createElement("style");
    style.textContent = ".stApp, body { background-color: var(--background) !important; }";
    container.appendChild(style);

    await waitFor(() => {
      expect(style.dataset.mewboScoped).toBe("true");
    });
    expect(scopedSelectors(style)).toContain(`${SCOPE} .stApp, ${SCOPE}`);
  });

  it("scopes <style> descendants of a subtree inserted after mount", async () => {
    const { getByTestId } = render(<Harness />);
    const container = getByTestId("container");

    // Streamlit typically appends a wrapper whose subtree contains the <style>.
    const wrapper = document.createElement("div");
    const style = document.createElement("style");
    style.textContent = "html, body { font-family: var(--font-sans) !important; }";
    wrapper.appendChild(style);
    container.appendChild(wrapper);

    await waitFor(() => {
      expect(style.dataset.mewboScoped).toBe("true");
    });
    expect(scopedSelectors(style)).toContain(`${SCOPE}, ${SCOPE}`);
  });
});
