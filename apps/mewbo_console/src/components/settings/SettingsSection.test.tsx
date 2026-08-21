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
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
        onSave={vi.fn().mockResolvedValue(true)}
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

describe("SettingsSection — a failed save must not announce success", () => {
  // Regression for the bug where `onSave` was typed `Promise<void>`: it
  // resolved without throwing on a failed PATCH (`useConfig.savePatch`
  // swallows the rejection and returns `null`), so `handleSave` stamped
  // `savedAt` unconditionally. That announced "Saved" to screen readers and
  // flipped `SecretField` into its post-save state (clearing whatever the
  // user had just typed) even though nothing was persisted. `onSave` now
  // resolves `false` on failure and `handleSave` must branch on it.
  test("a rejected onSave leaves the 'Saved' text and live-region announcement absent", async () => {
    const user = userEvent.setup();
    render(
      <SettingsSection
        model={model}
        sectionId="llm"
        // Differs from `original` so the section is dirty and Save is enabled.
        value={{ model: "gpt-5.5" }}
        original={{ model: "gpt-5" }}
        advanced={false}
        secrets={{}}
        onChange={vi.fn()}
        onSave={vi.fn().mockResolvedValue(false)}
      />
    );

    await user.click(screen.getByRole("button", { name: "Save" }));

    // `handleSave`'s `finally` always runs, so wait for the saving spinner to
    // clear rather than asserting immediately after the click.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Save" })).toBeInTheDocument()
    );

    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
    expect(document.querySelector('[aria-live="polite"].sr-only')?.textContent).toBe("");
  });
});

describe("SettingsSection — a sibling edit must not clobber an untouched secret", () => {
  // Reproduces a reported defect: an operator edited an unrelated model field and
  // saved, and the PATCH carried `llm.api_key: ""` over a live credential.
  // Root cause lives one layer below the widget: RJSF fills a MISSING
  // property with its JSON-schema `default` the instant a SIBLING field's
  // onChange fires, regardless of which widget owns that property — a custom
  // `ui:widget: "secret"` does not opt a field out of that fill. Every
  // x-secret/writeOnly field's Pydantic default is `""` (config.py), and the
  // backend never returns a secret's real value, so `api_key` starts this
  // test absent from `value`/`original`, exactly like the live wire shape.
  const secretSchema: Record<string, unknown> = {
    type: "object",
    title: "AppConfig",
    properties: { llm: { $ref: "#/$defs/LLMConfig" } },
    $defs: {
      LLMConfig: {
        type: "object",
        title: "LLM",
        "x-group": "models",
        "x-order": 1,
        properties: {
          default_model: { type: "string", title: "Model", default: "gpt-5.2" },
          api_key: { type: "string", title: "Api Key", default: "", "x-secret": true },
        },
      },
    },
  };
  const secretModel = new SettingsModel(secretSchema, {
    llm: { default_model: "gpt-5.2" }, // api_key key absent, as the API returns it
  });

  test("typing in the Model field never adds api_key to the emitted patch", async () => {
    const user = userEvent.setup();
    let latest: Record<string, unknown> = { default_model: "gpt-5.2" };
    const onChange = vi.fn((next: Record<string, unknown>) => {
      latest = next;
    });

    render(
      <SettingsSection
        model={secretModel}
        sectionId="llm"
        value={latest}
        original={{ default_model: "gpt-5.2" }}
        advanced={false}
        secrets={{}}
        onChange={onChange}
        onSave={vi.fn().mockResolvedValue(true)}
      />
    );

    await user.type(screen.getByLabelText("Model"), "x");

    // RJSF's own onChange payload DOES carry the schema-defaulted "" — that
    // part is RJSF, not this fix.
    expect(onChange).toHaveBeenCalled();
    const lastFormData = onChange.mock.calls[onChange.mock.calls.length - 1][0];
    expect(lastFormData.api_key).toBe("");

    // The fix is downstream, in what the model turns that formData into.
    const patch = secretModel.patchFor("llm", lastFormData);
    expect(patch).toEqual({ llm: { default_model: "gpt-5.2x" } });
  });
});
