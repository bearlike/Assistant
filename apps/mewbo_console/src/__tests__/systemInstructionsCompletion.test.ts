/**
 * The system-instructions editor's Jinja autocomplete.
 *
 * The source does not parse Jinja; it scans the text around the cursor for the
 * few shapes an operator actually types. That scanning is the part of the pane
 * most likely to be subtly wrong, so it is driven here through a real
 * `EditorState` + `CompletionContext` rather than through a rendered tooltip.
 *
 * `|` marks the cursor in every fixture below.
 */
import { describe, expect, test } from "vitest";
import { CompletionContext, type CompletionResult } from "@codemirror/autocomplete";
import { EditorState } from "@codemirror/state";

import { jinjaCompletionSource } from "../components/settings/panes/systemInstructionsCodeMirror";
import type { SystemInstructionsVariable } from "../api/systemInstructions";

const VARIABLES: SystemInstructionsVariable[] = [
  {
    name: "surface",
    type: "string",
    description: "The client surface that started this session.",
    values: ["cli", "console", "android"],
    valuesKind: "known",
    valuesNote: "Every surface that stamps a session today.",
  },
  {
    name: "capabilities",
    type: "array",
    description: "Capabilities granted to this session.",
    values: ["scg", "stlite", "wiki"],
    valuesKind: "known",
    valuesNote: "Declared by the plugins installed here.",
  },
  {
    name: "session_id",
    type: "string",
    description: "The id of the current session.",
    values: null,
    valuesKind: null,
    valuesNote: null,
  },
];

const source = jinjaCompletionSource(VARIABLES);

/** Run the source over `text`, with the cursor where the `|` marker sits. */
function complete(text: string): CompletionResult | null {
  const pos = text.indexOf("|");
  if (pos < 0) throw new Error("fixture has no `|` cursor marker");
  const doc = text.slice(0, pos) + text.slice(pos + 1);
  const state = EditorState.create({ doc });
  // `explicit: true` = the user asked for completions (Ctrl-Space), which is
  // also the only way to ask for them on an empty word.
  return source(new CompletionContext(state, pos, true));
}

const labels = (result: CompletionResult | null) => result?.options.map((o) => o.label) ?? [];

describe("jinja completion source", () => {
  test("offers nothing in prose outside a Jinja tag", () => {
    expect(complete("Keep answers short. surf|")).toBeNull();
    expect(complete("{% if surface %}hello sur|{% endif %}")).toBeNull();
  });

  test("offers variable names for a bare word inside a tag", () => {
    const result = complete("{% if sur| %}");
    expect(labels(result)).toEqual(["surface", "capabilities", "session_id"]);

    // Type and description ride along so the operator can pick without leaving
    // the editor for the reference table.
    const surface = result?.options.find((o) => o.label === "surface");
    expect(surface?.detail).toBe("string");
    expect(surface?.info).toBe("The client surface that started this session.");
  });

  test("offers variable names inside an expression tag too", () => {
    expect(labels(complete("{{ ses| }}"))).toContain("session_id");
  });

  test("infers the variable from an equality comparison to its left", () => {
    expect(labels(complete('{% if surface == "|" %}'))).toEqual(["cli", "console", "android"]);
    expect(labels(complete('{% if surface != "and|" %}'))).toEqual(["cli", "console", "android"]);
  });

  test("infers the variable from a membership test to its right", () => {
    // The string is written BEFORE the variable here, so the inference has to
    // look ahead of the cursor rather than behind it.
    expect(labels(complete('{% if "|" in capabilities %}'))).toEqual(["scg", "stlite", "wiki"]);
  });

  test("infers the variable from a membership list to its left", () => {
    expect(labels(complete('{% if surface in ["cli", "|"] %}'))).toEqual([
      "cli",
      "console",
      "android",
    ]);
  });

  test("replaces from inside the quote, so typed text filters instead of doubling", () => {
    const result = complete('{% if surface == "con|" %}');
    // The doc is `{% if surface == "con" %}`; the cursor sits at index 21, and
    // `from` must be 18 (just after the quote) so accepting `console` replaces
    // `con` rather than producing `conconsole`.
    expect(result?.from).toBe(18);
  });

  test("degrades to every candidate value, tagged by owner, when it cannot infer", () => {
    // A bare string with nothing to key off. Offering the union is always
    // truthful, where guessing one variable would sometimes be wrong.
    const result = complete('{% if "|" %}');
    expect(labels(result)).toEqual(["cli", "console", "android", "scg", "stlite", "wiki"]);
    expect(result?.options.find((o) => o.label === "scg")?.detail).toBe("capabilities");
    expect(result?.options.find((o) => o.label === "cli")?.detail).toBe("surface");
  });

  test("a closed string is not mistaken for an open one", () => {
    // `surface == "cli" and origin == "u|"` — the walk must not read the whole
    // span from the first quote as a single open string.
    const result = complete('{% if surface == "cli" and surface == "and|" %}');
    expect(labels(result)).toEqual(["cli", "console", "android"]);
  });

  test("a variable with no values degrades to the union rather than an empty menu", () => {
    // `session_id` is inferable here, but it has nothing to offer. Showing an
    // empty menu would read as "no legal values", which is the opposite of the
    // truth, so this falls through to the same union as an uninferable context.
    const result = complete('{% if session_id == "|" %}');
    expect(labels(result)).toEqual(["cli", "console", "android", "scg", "stlite", "wiki"]);
    expect(labels(result)).not.toContain("session_id");
  });
});
