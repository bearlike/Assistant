import { DiffFile } from "../types";
const DIFF_HEADER_OLD = "--- ";
const DIFF_HEADER_NEW = "+++ ";
function normalizePath(raw: string): string {
  const cleaned = raw.replace(/^[ab]\//, "").trim();
  if (!cleaned || cleaned === "/dev/null") {
    return "unknown";
  }
  return cleaned;
}
function deriveName(path: string): string {
  if (!path) {
    return "unknown";
  }
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}
export function extractUnifiedDiffs(text: string): DiffFile[] {
  if (!text) {
    return [];
  }
  const lines = text.split(/\r?\n/);
  const files: DiffFile[] = [];
  let current: DiffFile | null = null;
  let buffer: string[] = [];
  const finalize = () => {
    if (current && buffer.length) {
      current.diff = buffer.join("\n");
      files.push(current);
    }
    current = null;
    buffer = [];
  };
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (line.startsWith(DIFF_HEADER_OLD)) {
      const next = lines[i + 1];
      if (next && next.startsWith(DIFF_HEADER_NEW)) {
        finalize();
        const oldPath = normalizePath(line.slice(DIFF_HEADER_OLD.length));
        const newPath = normalizePath(next.slice(DIFF_HEADER_NEW.length));
        const path = newPath !== "unknown" ? newPath : oldPath;
        current = {
          path,
          name: deriveName(path),
          additions: 0,
          deletions: 0,
          diff: ""
        };
        buffer.push(line, next);
        i += 1;
        continue;
      }
    }
    if (current) {
      buffer.push(line);
      if (line.startsWith("+") && !line.startsWith(DIFF_HEADER_NEW)) {
        current.additions += 1;
      } else if (line.startsWith("-") && !line.startsWith(DIFF_HEADER_OLD)) {
        current.deletions += 1;
      }
    }
  }
  finalize();
  return files;
}
export function mergeDiffFiles(files: DiffFile[]): DiffFile[] {
  const merged = new Map<string, DiffFile>();
  for (const file of files) {
    const key = file.path || file.name;
    if (!merged.has(key)) {
      merged.set(key, {
        ...file,
        diff: file.diff || ""
      });
      continue;
    }
    const existing = merged.get(key);
    if (!existing) {
      continue;
    }
    existing.additions += file.additions;
    existing.deletions += file.deletions;
    const nextDiff = file.diff || "";
    if (nextDiff) {
      existing.diff = existing.diff ? `${existing.diff}\n${nextDiff}` : nextDiff;
    }
  }
  return Array.from(merged.values());
}