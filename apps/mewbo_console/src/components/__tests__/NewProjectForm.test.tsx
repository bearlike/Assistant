import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { NewProjectForm } from "../NewProjectForm";

afterEach(cleanup);

test("shows validation message when name is empty", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  render(<NewProjectForm onSubmit={onSubmit} onCancel={onCancel} />);

  await user.click(screen.getByRole("button", { name: /create project/i }));

  expect(await screen.findByText(/name is required/i)).toBeInTheDocument();
  expect(onSubmit).not.toHaveBeenCalled();
});

test("submits trimmed values when name is provided", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn().mockResolvedValue(undefined);
  const onCancel = vi.fn();
  render(<NewProjectForm onSubmit={onSubmit} onCancel={onCancel} />);

  await user.type(screen.getByLabelText(/name/i), "  My Workspace  ");
  await user.type(screen.getByLabelText(/description/i), "  helpful context ");
  await user.type(screen.getByLabelText(/path/i), "/tmp/work ");
  await user.click(screen.getByRole("button", { name: /create project/i }));

  await waitFor(() => {
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
  expect(onSubmit).toHaveBeenCalledWith({
    name: "My Workspace",
    description: "helpful context",
    path: "/tmp/work",
  });
});

test("cancel button invokes onCancel", async () => {
  const user = userEvent.setup();
  const onSubmit = vi.fn();
  const onCancel = vi.fn();
  render(<NewProjectForm onSubmit={onSubmit} onCancel={onCancel} />);

  await user.click(screen.getByRole("button", { name: /cancel/i }));
  expect(onCancel).toHaveBeenCalledTimes(1);
  expect(onSubmit).not.toHaveBeenCalled();
});
