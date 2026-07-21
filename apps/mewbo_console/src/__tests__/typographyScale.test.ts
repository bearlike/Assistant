import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join, relative } from "node:path";

/**
 * The console's typography guard.
 *
 * The console accumulated 987 font-size call sites across 16 distinct sizes,
 * roughly 350 of them arbitrary pixel literals, because nothing ever said no.
 * Review discipline alone demonstrably did not hold the line: the sizes did not
 * arrive in one bad commit, they arrived one reasonable-looking component at a
 * time. So the line is held here instead.
 *
 * Every font size must resolve to a step in `tailwind.config.js`
 * `theme.extend.fontSize`. If a surface genuinely needs a size the scale does
 * not have, the answer is to argue for a new STEP, not to inline a literal.
 */

const SRC = join(__dirname, "..");

/**
 * `text-[13px]` / `text-[0.8rem]` — an inlined ABSOLUTE size that bypasses the
 * scale.
 *
 * `em` is deliberately NOT banned. An `em` size is relative to its parent, so
 * it composes with whatever scale step the parent chose rather than overriding
 * it — inline `code` at `0.9em` stays correctly proportioned whether it sits in
 * a heading or in body prose, which an absolute value cannot do. The rule this
 * file enforces is "no size may escape the scale", and a relative size never
 * does.
 */
const ARBITRARY_SIZE = /\btext-\[[0-9.]+(?:px|rem|pt)\]/g;

/**
 * `--muted-foreground` composed with an alpha modifier **in text position**.
 * The token is tuned to sit at its WCAG AA floor on every surface it lands on,
 * so diluting it can only push it under — the alpha spellings this replaced
 * measured 2.18:1 in light mode and 3.99:1 in dark.
 *
 * Scoped to `text-` deliberately. The rule protects LEGIBILITY, so it applies
 * to glyphs; `decoration-`, `ring-`, `border-` and `bg-` are non-text and take
 * alpha legitimately (a strikethrough rule at 40% is a strikethrough, not
 * unreadable prose). An earlier, broader version of this rule was applied to a
 * scrollbar thumb and silently collapsed its `:hover` onto its base value,
 * deleting the affordance — over-applying a contrast rule causes its own bugs.
 *
 * Both spellings are caught: the CSS-function form
 * `text-[hsl(var(--muted-foreground)/0.5)]` and the Tailwind opacity modifier
 * `text-[hsl(var(--muted-foreground))]/50`. The modifier form is how this
 * codebase actually writes it, and an earlier regex that missed it reported
 * green while four live violations sat in the tree.
 */
const MUTED_ALPHA =
  /text-\[[^\]]*--muted-foreground[^\]]*\]\/\.?\d+|text-\[hsl\(var\(--muted-foreground\)\s*\/\s*\.?\d+\)\]/g;

/** Oversized display steps. The landing headline is `text-2xl`; nothing is larger. */
const OVERSIZED = /\btext-(?:3xl|4xl|5xl|6xl|7xl|8xl|9xl)\b/g;

function sourceFiles(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    // `__tests__/` includes this file, whose own regexes would otherwise match
    // themselves. Nothing else is exempt — mock fixtures render through the same
    // components as live data, so a size inlined there is a real one.
    if (entry === "__tests__" || entry === "node_modules") continue;
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) sourceFiles(full, acc);
    else if (/\.(tsx|ts)$/.test(full) && !/\.d\.ts$/.test(full)) acc.push(full);
  }
  return acc;
}

function scan(pattern: RegExp): string[] {
  const hits: string[] = [];
  for (const file of sourceFiles(SRC)) {
    const text = readFileSync(file, "utf8");
    text.split("\n").forEach((line, i) => {
      for (const match of line.matchAll(new RegExp(pattern.source, "g"))) {
        hits.push(`${relative(SRC, file)}:${i + 1}  ${match[0]}`);
      }
    });
  }
  return hits.sort();
}

describe("typography scale", () => {
  it("has no arbitrary font-size literals — every size resolves to a scale step", () => {
    const hits = scan(ARBITRARY_SIZE);
    expect(
      hits,
      `Arbitrary font sizes bypass the type scale.\n` +
        `Map them onto tailwind.config.js theme.extend.fontSize:\n` +
        `  10px, 11px -> text-2xs   12px -> text-xs   13px -> text-sm\n` +
        `  14px, 14.5px -> text-base   16px -> text-lg   20px -> text-xl   28px -> text-2xl\n` +
        `A text input needs text-field (16px floor, iOS zoom).\n\n` +
        hits.join("\n"),
    ).toEqual([]);
  });

  it("never composes --muted-foreground with an alpha modifier", () => {
    const hits = scan(MUTED_ALPHA);
    expect(
      hits,
      `--muted-foreground is tuned to sit at its WCAG AA floor on every surface ` +
        `it is used on. Diluting it with an alpha modifier pushes it under ` +
        `(measured 2.18:1 light, 3.99:1 dark). Use the bare token.\n\n` +
        hits.join("\n"),
    ).toEqual([]);
  });

  it("never lets a call site defeat the 16px input floor", () => {
    // iOS Safari zooms the viewport whenever a FOCUSED text input computes
    // below 16px. `text-base` is 14px here, so inputs carry `text-field`
    // (16px) instead, dropping to `md:text-sm` above the breakpoint where the
    // zoom cannot happen.
    //
    // The failure this catches is a CALL SITE passing its own `text-*` size.
    // `cn()` is last-wins, so any size from a caller overrides the floor and
    // silently re-opens the zoom on exactly the narrow viewports the floor
    // exists to protect. The primitive owns type size; callers pass layout.
    //
    // Matched by parsing each element's whole attribute span rather than by
    // line, because JSX props wrap across lines and a line-oriented pattern
    // reads a multi-line violation as clean.
    //
    // KNOWN BLIND SPOT: a size threaded in through a `className` PROP is
    // invisible here, because the literal never appears inside the element's
    // own JSX. `EditableTitle` is the known instance — its size arrives from
    // `SessionHeader`. A component that accepts a className and renders an
    // input must therefore apply the floor AFTER the incoming value
    // (`cn(className, "text-field md:text-sm")`), so last-wins puts the floor
    // on top. Do not read a green run here as proof that every input is safe.
    const INPUTISH = /<(input|textarea|Input|Textarea|CommandInput)\b/g;
    const BARE_SIZE = /(?<![\w:-])text-(?:2xs|xs|sm|base)\b/;

    const hits: string[] = [];
    for (const file of sourceFiles(SRC)) {
      const text = readFileSync(file, "utf8");
      for (const m of text.matchAll(INPUTISH)) {
        // Walk to the end of the opening tag, tracking brace depth so a `>`
        // inside an expression (an arrow function, a comparison) doesn't end
        // the span early.
        let depth = 0;
        // `matchAll` always populates `index`, but it is optional on the type;
        // read it once with a floor rather than asserting at three call sites.
        const start = m.index ?? 0;
        let end = start + m[0].length;
        for (; end < text.length; end++) {
          const c = text[end];
          if (c === "{") depth++;
          else if (c === "}") depth--;
          else if (c === ">" && depth === 0) break;
        }
        const attrs = text.slice(start, end);
        if (BARE_SIZE.test(attrs) && !attrs.includes("text-field")) {
          const line = text.slice(0, start).split("\n").length;
          hits.push(`${relative(SRC, file)}:${line}  <${m[1]}>`);
        }
      }
    }

    expect(
      hits.sort(),
      `These inputs compute below 16px, so iOS Safari zooms the viewport when ` +
        `they are focused.\nUse text-field (optionally with md:text-sm), and ` +
        `let the primitive own the size — a caller passing text-* wins under ` +
        `cn()'s last-wins merge and defeats the floor.\n\n` +
        hits.join("\n"),
    ).toEqual([]);
  });

  it("uses no display size above the landing headline", () => {
    const hits = scan(OVERSIZED);
    expect(
      hits,
      `text-2xl (28px) is the largest type in the console — it is an instrument ` +
        `panel, not a marketing page. Use text-2xl for landing headlines.\n\n` +
        hits.join("\n"),
    ).toEqual([]);
  });
});
