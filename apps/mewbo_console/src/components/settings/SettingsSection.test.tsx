/**
 * SettingsSection — regression test for the sr-only "Saved" live region
 * escaping its container.
 *
 * `sr-only` (Tailwind) is `position: absolute`. Its containing block is the
 * nearest POSITIONED ancestor; with none, that falls back to the initial
 * containing block (the document), which is what inflated
 * `document.documentElement.scrollHeight` to the span's own static offset
 * and made the whole app shell scroll past its `h-screen overflow-hidden` —
 * a wheel gesture then chained straight through the inner
 * `div.flex-1.overflow-y-auto` into a multi-thousand-pixel blank void. This
 * was proven LIVE in a browser (Playwright), not here: the void's height
 * tracked schema-section count per facet (Plugins ~15,464px, Workspace
 * ~3,838px, Agent ~3,278px; Security and Interface, which render zero
 * `SettingsSection`s, measured 0px).
 *
 * What THIS test can honestly prove: jsdom performs NO layout (no
 * `getBoundingClientRect`, no computed containing blocks, no scrollHeight),
 * so it cannot reproduce the void or confirm it's gone. What it CAN assert
 * is the DOM structure the fix relies on — that the sr-only span now sits
 * inside a `relative`-classed wrapper that SettingsSection itself supplies,
 * so the span gets a LOCAL containing block regardless of what
 * `SettingsCard` or the page shell around it does. That is a real, if
 * partial, regression guard: it fails if the wrapper is ever removed or the
 * span is hoisted back out of it, even though it can't fail on the actual
 * layout escape the way the live browser check did.
 */
import { cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { SettingsSection } from "./SettingsSection";
import { SettingsModel } from "./SettingsModel";

afterEach(cleanup);

const schema: Record<string, unknown> = {
  type: "object",
  title: "AppConfig",
  properties: {
    llm: { $ref: "#/$defs/LLMConfig" },
  },
  $defs: {
    LLMConfig: {
      type: "object",
      title: "LLM",
      "x-group": "models",
      "x-order": 1,
      properties: {
        model: { type: "string", title: "Model Name" },
      },
    },
  },
};

const model = new SettingsModel(schema, { llm: { model: "gpt-5" } });

describe("SettingsSection — sr-only live region containment", () => {
  test("the sr-only 'Saved' announcement has a relative ancestor SettingsSection owns", () => {
    render(
      <SettingsSection
        model={model}
        sectionId="llm"
        value={{ model: "gpt-5" }}
        original={{ model: "gpt-5" }}
        advanced={false}
        secrets={{}}
        onChange={vi.fn()}
        onSave={vi.fn().mockResolvedValue(undefined)}
      />
    );

    // The live region renders unconditionally (only its TEXT is conditional
    // on `savedAt`), so it's present pre-save too.
    const liveRegion = document.querySelector('[aria-live="polite"].sr-only');
    expect(liveRegion).not.toBeNull();

    // The fix: an immediate ancestor carries `relative`, giving the
    // absolutely-positioned span a local containing block instead of
    // escaping to the initial containing block (the document).
    const containingBlockAncestor = liveRegion?.parentElement;
    expect(containingBlockAncestor?.className).toContain("relative");
  });
});
