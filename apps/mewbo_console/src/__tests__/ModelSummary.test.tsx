import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test } from "vitest";
import { ModelSummary } from "../components/ModelSummary";

afterEach(cleanup);

describe("ModelSummary", () => {
  test("renders nothing when models is empty", () => {
    const { container } = render(<ModelSummary models={[]} />);
    expect(container.firstChild).toBeNull();
  });

  test("renders nothing when models is undefined", () => {
    const { container } = render(<ModelSummary />);
    expect(container.firstChild).toBeNull();
  });

  test("single model — shows model name, no '+N' indicator", () => {
    render(<ModelSummary models={["anthropic/claude-sonnet-4-6"]} />);
    // formatModelName strips the provider prefix
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    expect(screen.queryByText(/\+/)).toBeNull();
  });

  test("multiple models — trigger shows current model and '+N' badge", () => {
    render(
      <ModelSummary
        models={["anthropic/claude-sonnet-4-6", "openai/gpt-4o"]}
        current="anthropic/claude-sonnet-4-6"
      />
    );
    // Current model label present in trigger
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    // "+1" badge (2 models, so n-1 = 1)
    expect(screen.getByText(/\+1/)).toBeInTheDocument();
  });

  test("multiple models — trigger button has accessible aria-label", () => {
    render(
      <ModelSummary
        models={["anthropic/claude-sonnet-4-6", "openai/gpt-4o", "openai/gpt-4o-mini"]}
        current="anthropic/claude-sonnet-4-6"
      />
    );
    const button = screen.getByRole("button", { name: /3 models used/i });
    expect(button).toBeInTheDocument();
    // +2 indicator (3 models, n-1 = 2)
    expect(screen.getByText(/\+2/)).toBeInTheDocument();
  });

  test("multiple models — opening popover reveals all model names", async () => {
    const user = userEvent.setup();
    render(
      <ModelSummary
        models={["anthropic/claude-sonnet-4-6", "openai/gpt-4o"]}
        current="anthropic/claude-sonnet-4-6"
      />
    );
    const trigger = screen.getByRole("button", { name: /2 models used/i });
    await user.click(trigger);

    // Both model names should now be in the document (popover content rendered)
    expect(screen.getAllByText("claude-sonnet-4-6").length).toBeGreaterThan(0);
    expect(screen.getByText("gpt-4o")).toBeInTheDocument();
    // "current" annotation
    expect(screen.getByText("current")).toBeInTheDocument();
  });

  test("de-duplicates repeated model IDs", () => {
    render(
      <ModelSummary
        models={["anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4-6"]}
        current="anthropic/claude-sonnet-4-6"
      />
    );
    // After de-dupe, only 1 unique model → no popover trigger, no '+N'
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    expect(screen.queryByText(/\+/)).toBeNull();
  });
});
