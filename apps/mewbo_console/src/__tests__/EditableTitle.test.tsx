import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, test, vi } from "vitest";
import { EditableTitle } from "../components/EditableTitle";

afterEach(cleanup);

test("enters edit mode via pencil and saves on Enter", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn().mockResolvedValue(undefined);
  render(<EditableTitle value="Original" onSave={onSave} />);

  await user.click(screen.getByLabelText("Edit title"));
  const input = screen.getByLabelText("Edit session title") as HTMLInputElement;
  expect(input).toHaveValue("Original");

  await user.clear(input);
  await user.type(input, "Renamed");
  await user.keyboard("{Enter}");

  expect(onSave).toHaveBeenCalledWith("Renamed");
  expect(await screen.findByLabelText("Edit title")).toBeInTheDocument();
});

test("Escape cancels edit and restores original", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<EditableTitle value="Original" onSave={onSave} />);

  await user.click(screen.getByLabelText("Edit title"));
  const input = screen.getByLabelText("Edit session title") as HTMLInputElement;
  await user.clear(input);
  await user.type(input, "dropped");
  await user.keyboard("{Escape}");

  expect(onSave).not.toHaveBeenCalled();
  expect(screen.getByText("Original")).toBeInTheDocument();
});

test("empty input is treated as cancel", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<EditableTitle value="Original" onSave={onSave} />);

  await user.click(screen.getByLabelText("Edit title"));
  const input = screen.getByLabelText("Edit session title") as HTMLInputElement;
  await user.clear(input);
  await user.keyboard("{Enter}");

  expect(onSave).not.toHaveBeenCalled();
  expect(screen.getByText("Original")).toBeInTheDocument();
});

test("unchanged value does not call onSave", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<EditableTitle value="Same" onSave={onSave} />);

  await user.click(screen.getByLabelText("Edit title"));
  await user.keyboard("{Enter}");

  expect(onSave).not.toHaveBeenCalled();
});

test("save failure reverts to original value and stays editable", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn().mockRejectedValue(new Error("nope"));
  render(<EditableTitle value="Original" onSave={onSave} />);

  await user.click(screen.getByLabelText("Edit title"));
  const input = screen.getByLabelText("Edit session title") as HTMLInputElement;
  await user.clear(input);
  await user.type(input, "new");
  await user.keyboard("{Enter}");

  expect(onSave).toHaveBeenCalledWith("new");
  // Input stays visible after failure, showing original again.
  const retryInput = await screen.findByLabelText("Edit session title");
  expect((retryInput as HTMLInputElement).value).toBe("Original");
});

test("double-click on title enters edit mode", async () => {
  const user = userEvent.setup();
  const onSave = vi.fn();
  render(<EditableTitle value="Hello" onSave={onSave} />);

  await user.dblClick(screen.getByText("Hello"));
  expect(screen.getByLabelText("Edit session title")).toBeInTheDocument();
});
