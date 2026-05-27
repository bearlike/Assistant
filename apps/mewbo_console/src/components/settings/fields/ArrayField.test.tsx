/**
 * ArrayField.test — drives the custom `ArrayFieldTemplate` through a REAL
 * `@rjsf/core` <Form>, so we exercise RJSF's actual add/remove/reorder wiring
 * (the curried `onAddClick` / `onDropIndexClick` / `onReorderClick` handlers)
 * rather than a hand-built mock of the props.
 *
 * Harness mirrors SettingsView.integration.test.tsx: no Vitest `globals`, so
 * RTL auto-cleanup isn't registered — we `cleanup()` between tests explicitly.
 */
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test } from "vitest";
import Form from "@rjsf/core";
import validator from "@rjsf/validator-ajv8";
import type { RJSFSchema, UiSchema } from "@rjsf/utils";

import { ArrayFieldTemplate } from "./ArrayField";

const schema: RJSFSchema = {
  type: "array",
  items: { type: "string" },
  default: [],
};

function renderForm(opts?: { uiSchema?: UiSchema; formData?: unknown }) {
  return render(
    <Form
      schema={schema}
      validator={validator}
      templates={{ ArrayFieldTemplate }}
      uiSchema={opts?.uiSchema}
      formData={opts?.formData}
    />
  );
}

afterEach(cleanup);

describe("ArrayFieldTemplate", () => {
  test("clicking Add appends a row (an input appears)", async () => {
    renderForm();
    // Empty state first — no item inputs, and the muted "No entries." line.
    expect(screen.getByText("No entries.")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Add" }));

    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(screen.queryByText("No entries.")).toBeNull();
  });

  test("typing then clicking Remove drops the row", async () => {
    renderForm({ formData: ["alpha"] });
    const input = screen.getByDisplayValue("alpha");
    expect(input).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Remove entry" }));

    expect(screen.queryByDisplayValue("alpha")).toBeNull();
    expect(screen.getByText("No entries.")).toBeInTheDocument();
  });

  test("itemValidator surfaces an inline error for an empty row", async () => {
    renderForm({
      formData: [""],
      uiSchema: {
        "ui:options": {
          itemValidator: (v: string) => (v ? null : "required"),
        },
      },
    });
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("required");
  });

  test("Move-down on the first of two items swaps their order", async () => {
    renderForm({ formData: ["first", "second"] });

    const inputsBefore = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(inputsBefore.map((i) => i.value)).toEqual(["first", "second"]);

    // First row's "Move entry down" button (index 0).
    const rows = screen.getAllByRole("listitem");
    await userEvent.click(
      within(rows[0]).getByRole("button", { name: "Move entry down" })
    );

    const inputsAfter = screen.getAllByRole("textbox") as HTMLInputElement[];
    expect(inputsAfter.map((i) => i.value)).toEqual(["second", "first"]);
  });
});
