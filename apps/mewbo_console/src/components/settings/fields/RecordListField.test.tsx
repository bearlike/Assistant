/**
 * RecordListField — behavioral tests.
 *
 * Wires the field through a REAL `@rjsf/core` `<Form>` (not a bare render) with
 * the AJV validator, a `HookEntry` array schema, and `ui:field: "recordList"`,
 * mirroring the live wiring + the harness style of
 * `SettingsView.integration.test.tsx`. Asserts:
 *   - "Add hook" creates a card defaulting to command (Command visible, URL not);
 *   - switching the select to http reveals URL and hides Command;
 *   - typing a URL updates form data (observed via the Form `onChange` spy);
 *   - Remove drops the card.
 */
import {
  cleanup,
  render,
  screen,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Form from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";
import { afterEach, describe, expect, test, vi } from "vitest";

import { RecordListField } from "./RecordListField";

// --- the HookEntry array schema (the four HooksConfig arrays' shape) --------
const schema: RJSFSchema = {
  type: "array",
  title: "Hooks",
  items: {
    type: "object",
    properties: {
      type: { type: "string", enum: ["command", "http"], title: "Type" },
      command: { type: "string", title: "Command" },
      url: { type: "string", title: "URL" },
      headers: { type: "object", title: "Headers", additionalProperties: { type: "string" } },
      matcher: { type: ["string", "null"], title: "Matcher" },
      timeout: { type: "number", title: "Timeout" },
    },
  },
};

const uiSchema: UiSchema = { "ui:field": "recordList" };

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
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function renderForm(formData: any[] = []) {
  const onChange = vi.fn();
  render(
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    <Form<any>
      schema={schema}
      uiSchema={uiSchema}
      formData={formData}
      validator={validator}
      fields={{ recordList: RecordListField }}
      onChange={(e) => onChange(e.formData)}
    >
      <></>
    </Form>
  );
  return { onChange };
}

describe("RecordListField", () => {
  test("empty state shows the Add hook control and the empty message", () => {
    renderForm([]);
    expect(screen.getByText("No hooks configured.")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Add hook" })
    ).toBeInTheDocument();
  });

  test("Add hook creates a command-type card (Command visible, URL hidden)", async () => {
    renderForm([]);
    await userEvent.click(screen.getByRole("button", { name: "Add hook" }));

    // The type select defaults to command.
    const select = screen.getByRole("combobox") as HTMLSelectElement;
    expect(select.value).toBe("command");

    // Command is rendered; URL is not.
    expect(screen.getByLabelText("Command")).toBeInTheDocument();
    expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();
  });

  test("switching type to http reveals URL and hides Command", async () => {
    renderForm([
      {
        type: "command",
        command: "echo hi",
        url: "",
        headers: {},
        matcher: null,
        timeout: 30,
      },
    ]);

    expect(screen.getByLabelText("Command")).toBeInTheDocument();
    expect(screen.queryByLabelText("URL")).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByRole("combobox"), "http");

    expect(await screen.findByLabelText("URL")).toBeInTheDocument();
    expect(screen.queryByLabelText("Command")).not.toBeInTheDocument();
    // Headers (http-only) also appears.
    expect(screen.getByLabelText("Headers")).toBeInTheDocument();
  });

  test("typing a URL propagates to form data via onChange", async () => {
    const { onChange } = renderForm([
      {
        type: "http",
        command: "",
        url: "",
        headers: {},
        matcher: null,
        timeout: 30,
      },
    ]);

    const urlInput = screen.getByLabelText("URL");
    await userEvent.type(urlInput, "https://x.test/hook");

    // The latest onChange carries the typed URL on the single entry.
    const last = onChange.mock.calls.at(-1)?.[0] as Array<
      Record<string, unknown>
    >;
    expect(last[0].url).toBe("https://x.test/hook");
  });

  test("Remove drops the card", async () => {
    renderForm([
      {
        type: "command",
        command: "a",
        url: "",
        headers: {},
        matcher: null,
        timeout: 30,
      },
      {
        type: "command",
        command: "b",
        url: "",
        headers: {},
        matcher: null,
        timeout: 30,
      },
    ]);

    const cards = screen.getAllByRole("listitem");
    expect(cards).toHaveLength(2);

    // Remove the first card.
    const removeButtons = screen.getAllByRole("button", { name: "Remove hook" });
    await userEvent.click(removeButtons[0]);

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    // The surviving card is the second entry ("b").
    const remaining = screen.getByRole("listitem");
    expect(
      (within(remaining).getByLabelText("Command") as HTMLInputElement).value
    ).toBe("b");
  });
});
