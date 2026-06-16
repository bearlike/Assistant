/**
 * FileMentionPicker — the caret-anchored `@file` suggestion dropdown.
 *
 * Asserts the contract the composer relies on: when open, project files and
 * session attachments render as filterable rows, and selecting one hands the
 * raw path/name back to the parent (which splices it into the textarea).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";

afterEach(cleanup);

import { FileMentionPicker } from "../components/FileMentionPicker";

const FILES = ["src/app.ts", "src/components/InputBar.tsx", "README.md"];
const ATTACHMENTS = ["diagram.png"];

function renderPicker(over: Partial<React.ComponentProps<typeof FileMentionPicker>> = {}) {
  return render(
    <FileMentionPicker
      open
      query=""
      files={FILES}
      attachments={ATTACHMENTS}
      anchor={<div data-testid="anchor" />}
      onSelect={vi.fn()}
      onOpenChange={vi.fn()}
      {...over}
    />,
  );
}

describe("FileMentionPicker", () => {
  it("lists files and attachments when open with an empty query", () => {
    renderPicker();
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
    // Attachments render under their own group.
    expect(screen.getByText("Attachments")).toBeInTheDocument();
    expect(screen.getByText("diagram.png")).toBeInTheDocument();
  });

  it("filters the file list by the typed query (substring, path-aware)", () => {
    renderPicker({ query: "inputbar" });
    expect(screen.getByText("src/components/InputBar.tsx")).toBeInTheDocument();
    expect(screen.queryByText("README.md")).toBeNull();
  });

  it("returns the chosen path on select", () => {
    const onSelect = vi.fn();
    renderPicker({ query: "readme", onSelect });
    fireEvent.click(screen.getByText("README.md"));
    expect(onSelect).toHaveBeenCalledWith("README.md");
  });

  it("renders nothing selectable when closed", () => {
    renderPicker({ open: false });
    expect(screen.queryByText("src/app.ts")).toBeNull();
  });
});
