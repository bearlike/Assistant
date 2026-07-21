/**
 * CodeMirror 6 wiring for the system-instructions editor.
 *
 * The document mixes Markdown prose with Jinja control flow
 * (`{% if surface == "android" %}…{% endif %}`), and CM6 has no single
 * combined grammar for that pair — there is no dedicated Jinja package, and
 * the legacy `jinja2` mode (`@codemirror/legacy-modes/mode/jinja2`) is a
 * STANDALONE tokenizer (confirmed by reading the installed `.d.ts`/source: it
 * is `StreamParser<unknown>` with no `base`/backdrop parameter), so it can't
 * be nested inside Markdown's Lezer tree without a custom inline-parser
 * extension. Rather than either (a) losing prose highlighting by making
 * jinja2 the sole document language, or (b) writing a bespoke Lezer grammar
 * extension for a settings-pane nicety, this composes the two the way CM6's
 * own "custom syntax" examples do: `@codemirror/lang-markdown` is the
 * document `language` (headings/lists/emphasis/links/code), and a
 * `ViewPlugin` decoration layer HAND-DRIVES the real `jinja2` StreamParser
 * (`StringStream` + `.token()`, both public CM6 APIs) over the document text
 * to mark `{% %}` / `{{ }}` / `{# #}` spans on top of it. Both packages are
 * genuinely in use, not decorative dependencies.
 *
 * All colors ride the SAME `--hl-*` / `--code-*` token family that already
 * themes `TerminalCard`/`DiffCard` (`index.css`) — no new tokens, and the
 * editor adapts to light/dark for free since those tokens do.
 */
import {
  autocompletion,
  type Completion,
  type CompletionContext,
  type CompletionResult,
} from "@codemirror/autocomplete";
import { markdown } from "@codemirror/lang-markdown";
import { HighlightStyle, StringStream, syntaxHighlighting } from "@codemirror/language";
import { jinja2 } from "@codemirror/legacy-modes/mode/jinja2";
import { tags as t } from "@lezer/highlight";
import { RangeSetBuilder, type Extension } from "@codemirror/state";
import {
  Decoration,
  type DecorationSet,
  EditorView,
  ViewPlugin,
  type ViewUpdate,
} from "@codemirror/view";

import type { SystemInstructionsVariable } from "../../../api/systemInstructions";

// ---------------------------------------------------------------------------
// Markdown prose highlighting
// ---------------------------------------------------------------------------

const markdownHighlightStyle = HighlightStyle.define([
  {
    tag: [t.heading1, t.heading2, t.heading3, t.heading4, t.heading5, t.heading6],
    color: "hsl(var(--hl-function))",
    fontWeight: 600,
  },
  { tag: t.strong, fontWeight: 700, color: "hsl(var(--code-fg))" },
  { tag: t.emphasis, fontStyle: "italic" },
  { tag: [t.link, t.url], color: "hsl(var(--hl-builtin))", textDecoration: "underline" },
  { tag: t.monospace, color: "hsl(var(--hl-string))" },
  { tag: t.quote, color: "hsl(var(--hl-comment))", fontStyle: "italic" },
  { tag: [t.list, t.contentSeparator], color: "hsl(var(--hl-number))" },
  { tag: t.processingInstruction, color: "hsl(var(--code-fg-subtle))" },
  { tag: t.comment, color: "hsl(var(--hl-comment))", fontStyle: "italic" },
]);

// ---------------------------------------------------------------------------
// Jinja control-flow highlighting — the standalone `jinja2` legacy
// StreamParser, hand-driven line by line via the public `StringStream` API.
// ---------------------------------------------------------------------------

const JINJA_TOKEN_CLASS: Record<string, string> = {
  tag: "cm-jinja-tag",
  keyword: "cm-jinja-keyword",
  string: "cm-jinja-string",
  number: "cm-jinja-number",
  comment: "cm-jinja-comment",
  atom: "cm-jinja-atom",
  operator: "cm-jinja-operator",
  variable: "cm-jinja-variable",
};

// Guards a single line's tokenize loop against ever spinning forever if a
// future StreamParser change stops advancing `stream.pos` on some input.
const MAX_TOKENS_PER_LINE = 10_000;

function buildJinjaDecorations(view: EditorView): DecorationSet {
  const builder = new RangeSetBuilder<Decoration>();
  // `startState` is optional on the `StreamParser` interface; the vendored
  // jinja2 mode always defines it, but guard the type without a non-null
  // assertion.
  const state = jinja2.startState ? jinja2.startState(2) : {};
  const { doc } = view.state;
  for (let lineNo = 1; lineNo <= doc.lines; lineNo++) {
    const line = doc.line(lineNo);
    const stream = new StringStream(line.text, 2, 2);
    let guard = 0;
    while (!stream.eol() && guard++ < MAX_TOKENS_PER_LINE) {
      stream.start = stream.pos;
      const style = jinja2.token(stream, state);
      if (stream.pos === stream.start) {
        // Defensive: never let a zero-width token stall the loop.
        stream.next();
        continue;
      }
      const cls = style ? JINJA_TOKEN_CLASS[style] : undefined;
      if (cls) {
        builder.add(line.from + stream.start, line.from + stream.pos, Decoration.mark({ class: cls }));
      }
    }
  }
  return builder.finish();
}

const jinjaHighlightPlugin = ViewPlugin.fromClass(
  class {
    decorations: DecorationSet;
    constructor(view: EditorView) {
      this.decorations = buildJinjaDecorations(view);
    }
    update(update: ViewUpdate) {
      if (update.docChanged) this.decorations = buildJinjaDecorations(update.view);
    }
  },
  { decorations: (v) => v.decorations }
);

// ---------------------------------------------------------------------------
// Editor chrome
// ---------------------------------------------------------------------------

const editorTheme = EditorView.theme({
  "&": {
    backgroundColor: "hsl(var(--code-body))",
    color: "hsl(var(--code-fg))",
    fontSize: "var(--text-sm)",
  },
  ".cm-content": {
    fontFamily: "var(--font-mono)",
    caretColor: "hsl(var(--code-fg))",
  },
  ".cm-gutters": {
    backgroundColor: "hsl(var(--code-chrome))",
    color: "hsl(var(--code-fg-muted))",
    border: "none",
  },
  ".cm-activeLine": { backgroundColor: "hsl(var(--code-border))" },
  ".cm-activeLineGutter": { backgroundColor: "hsl(var(--code-border))" },
  "&.cm-focused .cm-selectionBackground, .cm-selectionBackground": {
    backgroundColor: "hsl(var(--primary) / 0.25)",
  },
  ".cm-cursor": { borderLeftColor: "hsl(var(--code-fg))" },
  "&.cm-editor.cm-focused": { outline: "none" },
});

// ---------------------------------------------------------------------------
// Autocomplete, driven by the SAME `/variables` payload that feeds the
// reference table. One source of truth: nothing about the variable set, its
// values, or its types is written down in this file.
//
// This deliberately does NOT parse Jinja. It answers two much smaller
// questions by scanning raw text around the cursor — "am I inside a `{{ }}` /
// `{% %}` tag?" and "am I inside a quoted string in one?" — and infers the
// owning variable from the few comparison shapes an operator actually types.
// Anything it can't place degrades to offering every candidate value, tagged
// with the variable that owns it, which is still a real help and can never be
// wrong. A grammar for this would be a lot of code to be occasionally more
// precise about a settings-pane convenience.
// ---------------------------------------------------------------------------

/**
 * How far either side of the cursor we look for a Jinja delimiter. A tag
 * longer than this is not something anyone hand-writes, and an unbounded scan
 * would walk the whole document on every keystroke.
 */
const JINJA_SCAN_CHARS = 400;

/** Cursor position relative to the enclosing Jinja tag, or null if in prose. */
interface JinjaSpan {
  /** Tag text from just after the `{{`/`{%` up to the cursor. */
  before: string;
  /** Tag text from the cursor up to the closing delimiter. */
  after: string;
}

/** Locate the `{{ }}` / `{% %}` tag the cursor sits inside, if any. */
function jinjaSpanAt(context: CompletionContext): JinjaSpan | null {
  const { state, pos } = context;
  const lead = state.sliceDoc(Math.max(0, pos - JINJA_SCAN_CHARS), pos);

  // The cursor is inside a tag iff the nearest opening delimiter behind it is
  // not already closed. `{#` comments are intentionally not offered in.
  const open = Math.max(lead.lastIndexOf("{{"), lead.lastIndexOf("{%"));
  if (open < 0) return null;
  const close = Math.max(lead.lastIndexOf("}}"), lead.lastIndexOf("%}"));
  if (close > open) return null;

  const trail = state.sliceDoc(pos, Math.min(state.doc.length, pos + JINJA_SCAN_CHARS));
  const closeAhead = trail.search(/\}\}|%\}/);
  return {
    before: lead.slice(open + 2),
    after: closeAhead < 0 ? trail : trail.slice(0, closeAhead),
  };
}

/**
 * Index of the opening quote of an unterminated string literal in `text`, or
 * -1 when the cursor is not inside one. Walking the whole tag (rather than
 * matching the last quote) is what keeps `surface == "cli" and origin == "u|"`
 * from reading as one long string.
 */
function openStringFrom(text: string): number {
  let quote = "";
  let from = -1;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quote) {
      if (ch === quote) {
        quote = "";
        from = -1;
      }
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      from = i;
    }
  }
  return from;
}

/** `surface == "…"` / `surface != "…"` — the variable is left of the string. */
const COMPARISON_LHS = /([A-Za-z_]\w*)\s*[=!]=\s*$/;
/** `surface in ["cli", "…"]` — the variable is left of a membership list. */
const MEMBERSHIP_LHS = /([A-Za-z_]\w*)\s+in\s+[[(]?\s*(?:(?:"[^"]*"|'[^']*')\s*,\s*)*$/;
/**
 * `"scg" in capabilities` — the variable is RIGHT of the string. The optional
 * quote is load-bearing: the cursor is INSIDE the literal, so the text ahead of
 * it opens with that literal's own closing quote before the ` in …` arrives.
 */
const MEMBERSHIP_RHS = /^["']?\s*in\s+([A-Za-z_]\w*)/;

/**
 * Which variable's values belong in the string literal the cursor is in, from
 * the three shapes that cover essentially every comparison an operator writes.
 * Null when none of them match, which is the caller's cue to degrade.
 */
function inferValueOwner(
  beforeString: string,
  afterCursor: string,
  byName: Map<string, SystemInstructionsVariable>
): SystemInstructionsVariable | null {
  const name =
    beforeString.match(COMPARISON_LHS)?.[1] ??
    beforeString.match(MEMBERSHIP_LHS)?.[1] ??
    afterCursor.match(MEMBERSHIP_RHS)?.[1];
  const owner = name ? byName.get(name) : undefined;
  return owner?.values?.length ? owner : null;
}

/** Model ids and project names carry `/`, `.`, `-`, so they can't use `\w`. */
const VALUE_CHARS = /^[\w./:-]*$/;

function valueCompletions(variable: SystemInstructionsVariable): Completion[] {
  return (variable.values ?? []).map((value) => ({
    label: value,
    type: "constant",
    detail: variable.name,
  }));
}

/**
 * The one completion source. Inside a Jinja tag it offers variable NAMES for a
 * bare word, and VALUES inside a string literal.
 *
 * Exported for its own tests: the text scanning above is the part of this file
 * most likely to be wrong, and driving it through a real `EditorState` is far
 * cheaper than asserting on a rendered completion tooltip.
 */
export function jinjaCompletionSource(variables: SystemInstructionsVariable[]) {
  const byName = new Map(variables.map((v) => [v.name, v]));
  const nameOptions: Completion[] = variables.map((v) => ({
    label: v.name,
    type: "variable",
    detail: v.type,
    info: v.description,
  }));
  // The degraded offer: every candidate value in the payload, each tagged with
  // the variable it came from so the list stays legible even unfiltered.
  const allValueOptions: Completion[] = variables.flatMap(valueCompletions);

  return (context: CompletionContext): CompletionResult | null => {
    const span = jinjaSpanAt(context);
    if (!span) return null;

    const quoteAt = openStringFrom(span.before);
    if (quoteAt >= 0) {
      const owner = inferValueOwner(span.before.slice(0, quoteAt), span.after, byName);
      const options = owner ? valueCompletions(owner) : allValueOptions;
      if (options.length === 0) return null;
      // Replace from just after the quote, so what has been typed inside it
      // filters the list instead of being prepended to the chosen value.
      const typed = span.before.length - quoteAt - 1;
      return { from: context.pos - typed, options, validFor: VALUE_CHARS };
    }

    const word = context.matchBefore(/[A-Za-z_]\w*/);
    if (!word || (word.from === word.to && !context.explicit)) return null;
    if (nameOptions.length === 0) return null;
    return { from: word.from, options: nameOptions, validFor: /^\w*$/ };
  };
}

/**
 * The full extension set for the system-instructions editor.
 *
 * Takes the variables because completion is built from them: call it inside a
 * `useMemo` keyed on the payload so the editor is reconfigured when it arrives,
 * and never captures an empty list from the first render.
 *
 * `autocompletion` is owned here rather than left to `basicSetup` (which
 * enables it with no sources). The pane therefore turns basicSetup's copy OFF,
 * so there is exactly one autocompletion config in the editor and no
 * dependence on how two of them would merge.
 */
export function systemInstructionsExtensions(
  variables: SystemInstructionsVariable[]
): Extension[] {
  return [
    markdown(),
    syntaxHighlighting(markdownHighlightStyle),
    jinjaHighlightPlugin,
    editorTheme,
    EditorView.lineWrapping,
    autocompletion({ override: [jinjaCompletionSource(variables)] }),
  ];
}
