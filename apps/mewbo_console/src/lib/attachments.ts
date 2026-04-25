/**
 * Shared client-side attachment policy.
 *
 * Mirrors ``mewbo_core.attachments`` on the backend. When you change one
 * side, change the other — the backend is the safety net (rejects on
 * upload), this is the UX layer (filters at file selection).
 */

/** MIME types accepted as documents (parsed to Markdown server-side). */
export const DOCUMENT_MIME_TYPES = new Set<string>([
  "application/pdf",
  "application/msword",
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  "application/vnd.ms-excel",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  "application/vnd.ms-powerpoint",
  "application/vnd.openxmlformats-officedocument.presentationml.presentation",
  "text/csv",
  "text/plain",
  "text/markdown",
  "application/json",
  "application/xml",
  "text/xml",
  "application/x-yaml",
  "text/yaml",
  "text/html",
]);

/** MIME types accepted as images (sent inline to vision-capable models). */
export const IMAGE_MIME_TYPES = new Set<string>([
  "image/png",
  "image/jpeg",
  "image/jpg",
  "image/gif",
  "image/webp",
]);

/** Extensions checked when a browser sends ``application/octet-stream``. */
const SUPPORTED_EXTENSIONS = new Set<string>([
  ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
  ".csv", ".txt", ".md", ".json", ".xml", ".yaml", ".yml", ".html",
  ".png", ".jpg", ".jpeg", ".gif", ".webp",
]);

/**
 * The HTML ``accept`` attribute for the file input. Strict — only files
 * matching one of these MIME types/extensions appear in the picker.
 */
export const FILE_INPUT_ACCEPT =
  "application/pdf," +
  ".doc,.docx,.xls,.xlsx,.ppt,.pptx," +
  "text/csv,text/plain,text/markdown," +
  "application/json,application/xml,text/xml," +
  "application/x-yaml,text/yaml,text/html," +
  "image/png,image/jpeg,image/gif,image/webp";

function fileExt(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot >= 0 ? name.slice(dot).toLowerCase() : "";
}

/** True if the file's MIME type is an image (or extension fallback). */
export function isImage(file: File): boolean {
  const t = (file.type || "").toLowerCase();
  if (IMAGE_MIME_TYPES.has(t)) return true;
  const ext = fileExt(file.name);
  return ext === ".png" || ext === ".jpg" || ext === ".jpeg" ||
    ext === ".gif" || ext === ".webp";
}

/** True if the file is one of our supported types. */
export function isSupported(file: File): boolean {
  const t = (file.type || "").toLowerCase();
  if (DOCUMENT_MIME_TYPES.has(t) || IMAGE_MIME_TYPES.has(t)) return true;
  if (t.startsWith("text/")) return true;
  return SUPPORTED_EXTENSIONS.has(fileExt(file.name));
}

export type ModelCapMap = Record<string, { supports_vision: boolean }>;

export type AttachmentFilterResult = {
  accepted: File[];
  rejected: { file: File; reason: string }[];
};

/**
 * Partition incoming files into accepted/rejected based on type support
 * and (optionally) the active model's vision capability.
 *
 * Used by the InputBar before pushing files onto the upload queue —
 * surfaces a single combined toast for everything that didn't fit.
 */
export function filterAttachments(
  files: File[],
  options: { model?: string | null; capabilities?: ModelCapMap } = {},
): AttachmentFilterResult {
  const accepted: File[] = [];
  const rejected: { file: File; reason: string }[] = [];
  const supportsVision = options.model
    ? !!options.capabilities?.[options.model]?.supports_vision
    : true; // Unknown model — let the backend decide.
  for (const file of files) {
    if (!isSupported(file)) {
      rejected.push({ file, reason: `unsupported type (${file.type || "unknown"})` });
      continue;
    }
    if (isImage(file) && options.model && !supportsVision) {
      rejected.push({
        file,
        reason: `model "${options.model}" does not support images`,
      });
      continue;
    }
    accepted.push(file);
  }
  return { accepted, rejected };
}
