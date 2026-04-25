import { describe, expect, it } from "vitest";
import {
  filterAttachments,
  isImage,
  isSupported,
  FILE_INPUT_ACCEPT,
} from "../lib/attachments";

function file(name: string, type: string): File {
  return new File([new Uint8Array(8)], name, { type });
}

describe("isImage", () => {
  it("recognises image MIME types", () => {
    expect(isImage(file("a.png", "image/png"))).toBe(true);
    expect(isImage(file("a.jpg", "image/jpeg"))).toBe(true);
    expect(isImage(file("a.pdf", "application/pdf"))).toBe(false);
  });
  it("falls back to extension when MIME is missing", () => {
    expect(isImage(file("a.png", ""))).toBe(true);
    expect(isImage(file("a.txt", ""))).toBe(false);
  });
});

describe("isSupported", () => {
  it("accepts known document MIME types", () => {
    expect(isSupported(file("a.pdf", "application/pdf"))).toBe(true);
    expect(isSupported(file("a.csv", "text/csv"))).toBe(true);
  });
  it("falls back to extension for octet-stream", () => {
    expect(
      isSupported(file("a.docx", "application/octet-stream")),
    ).toBe(true);
  });
  it("rejects unsupported binaries", () => {
    expect(isSupported(file("a.exe", "application/x-msdownload"))).toBe(false);
  });
});

describe("filterAttachments", () => {
  it("keeps supported files", () => {
    const out = filterAttachments([file("a.pdf", "application/pdf")]);
    expect(out.accepted).toHaveLength(1);
    expect(out.rejected).toHaveLength(0);
  });

  it("rejects unsupported types with a reason", () => {
    const out = filterAttachments([file("a.exe", "application/x-msdownload")]);
    expect(out.accepted).toHaveLength(0);
    expect(out.rejected[0].reason).toMatch(/unsupported/i);
  });

  it("rejects images on non-vision models", () => {
    const out = filterAttachments(
      [file("pic.png", "image/png")],
      {
        model: "text-only",
        capabilities: { "text-only": { supports_vision: false } },
      },
    );
    expect(out.accepted).toHaveLength(0);
    expect(out.rejected[0].reason).toMatch(/does not support images/);
  });

  it("accepts images on vision-capable models", () => {
    const out = filterAttachments(
      [file("pic.png", "image/png")],
      {
        model: "vision-model",
        capabilities: { "vision-model": { supports_vision: true } },
      },
    );
    expect(out.accepted).toHaveLength(1);
    expect(out.rejected).toHaveLength(0);
  });

  it("defers to backend when model is unknown", () => {
    // No model passed → don't second-guess; let the upload step decide.
    const out = filterAttachments([file("pic.png", "image/png")]);
    expect(out.accepted).toHaveLength(1);
  });
});

describe("FILE_INPUT_ACCEPT", () => {
  it("includes representative image and document types", () => {
    expect(FILE_INPUT_ACCEPT).toContain("application/pdf");
    expect(FILE_INPUT_ACCEPT).toContain("image/png");
    expect(FILE_INPUT_ACCEPT).toContain(".docx");
  });
});
