/**
 * Tests for SecretField — the write-only secret widget's three states.
 *
 * The widget is rendered directly with a minimal `WidgetProps` stub (no full
 * RJSF form) so the state machine is exercised in isolation:
 *   - unconfigured → password input
 *   - configured   → masked indicator + Replace
 *   - Replace      → password input
 *   - Cancel       → onChange(undefined)
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";
import type { WidgetProps } from "@rjsf/utils";

import { SecretField } from "./SecretField";

afterEach(cleanup);

/** Build a minimal WidgetProps for the widget under test. */
function makeProps(overrides: Partial<WidgetProps> = {}): WidgetProps {
  return {
    id: "root_api_key",
    name: "api_key",
    label: "API Key",
    value: undefined,
    disabled: false,
    readonly: false,
    onChange: vi.fn(),
    options: {},
    ...overrides,
  } as unknown as WidgetProps;
}

describe("SecretField", () => {
  test("unconfigured → renders an empty password input", () => {
    const { container } = render(<SecretField {...makeProps()} />);
    const input = container.querySelector<HTMLInputElement>("#root_api_key");
    expect(input).not.toBeNull();
    expect(input?.type).toBe("password");
    expect(input?.value).toBe("");
    expect(input?.getAttribute("autocomplete")).toBe("new-password");
    expect(screen.queryByRole("button", { name: "Replace" })).toBeNull();
  });

  test("unconfigured → typing calls onChange with the value", async () => {
    const onChange = vi.fn();
    const { container } = render(
      <SecretField {...makeProps({ onChange })} />
    );
    const input = container.querySelector<HTMLInputElement>("#root_api_key");
    expect(input).not.toBeNull();
    await userEvent.type(input as HTMLInputElement, "x");
    expect(onChange).toHaveBeenCalledWith("x");
  });

  test("configured → renders masked indicator + Replace, no input", () => {
    const { container } = render(
      <SecretField {...makeProps({ options: { secretConfigured: true } })} />
    );
    expect(container.querySelector("#root_api_key")).toBeNull();
    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Replace" })
    ).toBeInTheDocument();
  });

  test("configured → clicking Replace reveals the password input", async () => {
    const { container } = render(
      <SecretField {...makeProps({ options: { secretConfigured: true } })} />
    );
    await userEvent.click(screen.getByRole("button", { name: "Replace" }));
    const input = container.querySelector<HTMLInputElement>("#root_api_key");
    expect(input).not.toBeNull();
    expect(input?.type).toBe("password");
    expect(
      screen.getByRole("button", { name: "Cancel" })
    ).toBeInTheDocument();
  });

  test("editing → Cancel reverts the value via onChange(undefined)", async () => {
    const onChange = vi.fn();
    render(
      <SecretField
        {...makeProps({ onChange, options: { secretConfigured: true } })}
      />
    );
    await userEvent.click(screen.getByRole("button", { name: "Replace" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onChange).toHaveBeenCalledWith(undefined);
    // Returns to the masked/Replace state.
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
  });

  test("post-save → a bumped savedAt leaves editing for the masked state", async () => {
    // Unconfigured: user types a new value, then Saves. The section re-seeds
    // value=undefined (secrets are never returned) but bumps savedAt; the
    // secrets map now reports the field as configured. Without the reset the
    // widget would dangle in an empty editing input; with it, it snaps to
    // masked "Configured".
    const { rerender, container } = render(
      <SecretField {...makeProps({ options: { secretConfigured: false, savedAt: 0 } })} />
    );
    // Pre-save: an editable input, no Configured indicator.
    expect(container.querySelector("#root_api_key")).not.toBeNull();
    expect(screen.queryByText("Configured")).toBeNull();

    rerender(
      <SecretField
        {...makeProps({ value: undefined, options: { secretConfigured: true, savedAt: 1 } })}
      />
    );

    expect(screen.getByText("Configured")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
    expect(container.querySelector("#root_api_key")).toBeNull();
  });

  test("post-save → bumped savedAt exits an active editing input", async () => {
    // Configured secret: user clicks Replace (now editing), then Saves.
    const { rerender, container } = render(
      <SecretField {...makeProps({ options: { secretConfigured: true, savedAt: 0 } })} />
    );
    await userEvent.click(screen.getByRole("button", { name: "Replace" }));
    expect(container.querySelector("#root_api_key")).not.toBeNull();

    rerender(
      <SecretField {...makeProps({ options: { secretConfigured: true, savedAt: 1 } })} />
    );

    // Back to masked/Replace — no dangling editing input or Cancel button.
    expect(screen.getByRole("button", { name: "Replace" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel" })).toBeNull();
    expect(container.querySelector("#root_api_key")).toBeNull();
  });
});
