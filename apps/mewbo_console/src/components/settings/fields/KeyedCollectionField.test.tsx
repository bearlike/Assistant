/**
 * KeyedCollectionField — behavioral tests.
 *
 * Wires the field through a REAL `@rjsf/core` `<Form>` (mirroring
 * `RecordListField.test.tsx`) with three `additionalProperties` shapes that
 * exercise the three value renderers + the headline-is-the-key rename behavior:
 *
 *   1. integer  → number renderer: Add creates an entry whose headline shows the
 *      key; entering a number propagates a number (not a string).
 *   2. object   → JSON renderer: invalid JSON shows the error and does NOT
 *      propagate; valid JSON does.
 *   3. rename + remove → re-keying preserves data; Remove drops an entry.
 */
import {
  cleanup,
  fireEvent,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Form from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { afterEach, describe, expect, test, vi } from "vitest";

import { KeyedCollectionField } from "./KeyedCollectionField";

const uiSchema: UiSchema = { "ui:field": "keyedCollection" };

// This project does not enable Vitest `globals`, so RTL's auto-cleanup is not
// registered — unmount each render explicitly between tests.
afterEach(cleanup);

/**
 * Render the field through a real RJSF Form; returns the onChange spy.
 *
 * `formData` is left untyped (`any`) so the `<Form>` generic infers `T = any`,
 * matching the live `SettingsSection` wiring where the dynamic backend schema
 * means RJSF is never statically typed to a concrete form shape.
 */
function renderForm(
  schema: RJSFSchema,
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  formData: any = {}
) {
  const onChange = vi.fn();
  render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Form<any>
      schema={schema}
      uiSchema={uiSchema}
      formData={formData}
      validator={validator}
      fields={{ keyedCollection: KeyedCollectionField }}
      onChange={(e) => onChange(e.formData)}
    >
      <></>
    </Form>
  );
  return { onChange };
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function lastFormData(onChange: ReturnType<typeof vi.fn>): any {
  return onChange.mock.calls.at(-1)?.[0];
}

describe("KeyedCollectionField", () => {
  const numberSchema: RJSFSchema = {
    type: "object",
    additionalProperties: { type: "integer" },
  };
  const objectSchema: RJSFSchema = {
    type: "object",
    additionalProperties: { type: "object" },
  };

  test("number entries: Add shows key as headline; values propagate as numbers", async () => {
    const { onChange } = renderForm(numberSchema, {});

    expect(screen.getByText("No entries configured.")).toBeInTheDocument();

    // Open the Add dialog, type a name, create.
    await userEvent.click(screen.getByRole("button", { name: "Add entry" }));
    await userEvent.type(
      screen.getByLabelText("Key for new entry"),
      "gpt"
    );
    await userEvent.click(screen.getByRole("button", { name: "Create" }));

    // The new entry's headline (rename) input shows the key "gpt".
    const headline = screen.getByLabelText("Key for gpt") as HTMLInputElement;
    expect(headline.value).toBe("gpt");

    // Enter a value; it must propagate as a NUMBER, not a string.
    const valueInput = screen.getByLabelText("Value for gpt");
    await userEvent.type(valueInput, "123");

    const data = lastFormData(onChange);
    expect(data.gpt).toBe(123);
    expect(typeof data.gpt).toBe("number");
  });

  test("json entries: invalid JSON shows an error and does not propagate; valid does", async () => {
    const { onChange } = renderForm(objectSchema, { web: {} });

    const editor = screen.getByLabelText("Key for web"); // entry exists
    expect((editor as HTMLInputElement).value).toBe("web");

    // The JSON textarea is the lone textbox in the card.
    const card = screen.getByRole("listitem");
    const textarea = within(card).getByRole("textbox", {
      name: "",
    }) as HTMLTextAreaElement;

    // Invalid JSON → inline error, no propagation.
    onChange.mockClear();
    fireEvent.change(textarea, { target: { value: "{ not valid" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Invalid JSON.");
    expect(onChange).not.toHaveBeenCalled();

    // Valid JSON → error clears, value propagates.
    fireEvent.change(textarea, { target: { value: '{"token":"x"}' } });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    const data = lastFormData(onChange);
    expect(data.web).toEqual({ token: "x" });
  });

  test("rename re-keys the entry; Remove drops it", async () => {
    const { onChange } = renderForm(numberSchema, { a: 1, b: 2 });

    expect(screen.getAllByRole("listitem")).toHaveLength(2);

    // Rename "a" → "alpha": change + blur.
    const headlineA = screen.getByLabelText("Key for a") as HTMLInputElement;
    fireEvent.change(headlineA, { target: { value: "alpha" } });
    fireEvent.blur(headlineA);

    let data = lastFormData(onChange);
    expect("a" in data).toBe(false);
    expect(data.alpha).toBe(1);
    expect(data.b).toBe(2);

    // Remove "b".
    onChange.mockClear();
    await userEvent.click(screen.getByRole("button", { name: "Remove b" }));
    data = lastFormData(onChange);
    expect("b" in data).toBe(false);
    expect(data.alpha).toBe(1);
  });
});
