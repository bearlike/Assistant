/**
 * FieldHelp — regression coverage for the collapse rule (see the block
 * comment atop FieldHelp.tsx for the three cases this pins down).
 *
 * The original bug: `firstLine = text.split("\n")[0]` on a description with
 * NO newline is the entire string, so a long single-line description (e.g.
 * GitCredentialsView's original 350-char intro) rendered inline AND, again,
 * verbatim inside the popover. `no-break-long` below is the exact
 * regression test for that.
 */
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test } from "vitest";

import { FieldHelp } from "./FieldHelp";

afterEach(cleanup);

describe("FieldHelp", () => {
  test("empty/undefined text renders nothing", () => {
    const { container: a } = render(<FieldHelp text={undefined} />);
    expect(a.firstChild).toBeNull();
    cleanup();
    const { container: b } = render(<FieldHelp text="   " />);
    expect(b.firstChild).toBeNull();
  });

  test("short, no newline (<= 140 chars) — renders inline, no popover trigger", () => {
    const text = "Short helper text under the collapse threshold.";
    expect(text.length).toBeLessThanOrEqual(140);
    render(<FieldHelp text={text} />);

    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More info" })).toBeNull();
  });

  test("exactly 140 chars, no newline — still the inline no-popover case", () => {
    const text = "x".repeat(140);
    render(<FieldHelp text={text} />);

    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "More info" })).toBeNull();
  });

  test("multi-line — first line inline, remainder behind the popover (existing behavior)", async () => {
    const firstLine = "Summary line the author wrote.";
    const rest =
      "Second paragraph with a lot more detail that only shows up once the reader opens the popover.";
    const text = `${firstLine}\n\n${rest}`;
    render(<FieldHelp text={text} />);

    // First line renders inline immediately.
    expect(screen.getByText(firstLine)).toBeInTheDocument();
    // The trailing paragraph is NOT on the page yet (popover closed).
    expect(screen.queryByText(rest)).toBeNull();

    const trigger = screen.getByRole("button", { name: "More info" });
    await userEvent.click(trigger);

    // Popover content carries the full text, including the rest paragraph.
    expect(await screen.findByText(rest)).toBeInTheDocument();
  });

  test("long single line, no newline — regression: never renders the same text twice", async () => {
    const text =
      "Tokens and SSH keys Mewbo uses to clone and fetch private repositories from every host " +
      "you have configured, including hosts that require a scoped personal access token instead " +
      "of a bare password.";
    expect(text).not.toContain("\n");
    expect(text.length).toBeGreaterThan(140);
    render(<FieldHelp text={text} />);

    // Nothing inline yet — no natural break to summarize on, so there is
    // deliberately no fabricated first line before the popover opens.
    expect(screen.queryByText(text)).toBeNull();
    expect(screen.queryByText(text, { exact: false })).toBeNull();

    const trigger = screen.getByRole("button", { name: "More info" });
    expect(trigger).toBeInTheDocument();

    await userEvent.click(trigger);

    // The full text now appears exactly once, inside the popover.
    const matches = await screen.findAllByText(text);
    expect(matches).toHaveLength(1);
  });

  test("markdown renders in the short inline case", () => {
    render(<FieldHelp text="Uses **bold** and `code` inline." />);
    expect(screen.getByText("bold").tagName).toBe("STRONG");
    expect(screen.getByText("code").tagName).toBe("CODE");
  });

  test("markdown renders inside the popover for the no-break-long case", async () => {
    const text =
      "A very long single-line description with **bold emphasis** buried inside it that pushes " +
      "the whole string well past the inline collapse threshold so it only ever appears in the popover.";
    expect(text).not.toContain("\n");
    expect(text.length).toBeGreaterThan(140);
    render(<FieldHelp text={text} />);

    await userEvent.click(screen.getByRole("button", { name: "More info" }));

    const bold = await screen.findByText("bold emphasis");
    expect(bold.tagName).toBe("STRONG");
  });
});
