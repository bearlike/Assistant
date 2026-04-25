import { parse } from "diff2html";
import { DiffFile, ParsedDiffFile } from "../types";

function deriveName(path: string): string {
  if (!path) return "unknown";
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

/**
 * Extract file-level diff info from unified diff text.
 * Backward-compatible wrapper around diff2html's parse().
 */
export function extractUnifiedDiffs(text: string): DiffFile[] {
  if (!text) return [];
  let parsed: ReturnType<typeof parse>;
  try {
    parsed = parse(text);
  } catch {
    return [];
  }
  return parsed.map((f) => ({
    name: deriveName(f.newName || f.oldName),
    path: f.newName || f.oldName,
    additions: f.addedLines,
    deletions: f.deletedLines,
    diff: reconstructRawDiff(f),
  }));
}

/** Reconstruct raw unified diff text from parsed blocks for DiffView compatibility. */
function reconstructRawDiff(
  f: ReturnType<typeof parse>[number]
): string {
  const lines: string[] = [];
  lines.push(`--- ${f.oldName}`);
  lines.push(`+++ ${f.newName}`);
  for (const block of f.blocks) {
    lines.push(block.header);
    for (const line of block.lines) {
      lines.push(line.content);
    }
  }
  return lines.join("\n");
}

/**
 * Parse unified diff text into rich hunk/line data for DiffCard rendering.
 * Each line includes oldNumber/newNumber for the two-column gutter.
 */
export function parseDiffHunks(text: string): ParsedDiffFile[] {
  if (!text) return [];
  let parsed: ReturnType<typeof parse>;
  try {
    parsed = parse(text);
  } catch {
    return [];
  }
  return parsed.map((f) => ({
    name: deriveName(f.newName || f.oldName),
    path: f.newName || f.oldName,
    additions: f.addedLines,
    deletions: f.deletedLines,
    isNewFile: !!f.isNew,
    isDeleted: !!f.isDeleted,
    hunks: f.blocks.map((b) => ({
      header: b.header,
      lines: b.lines.map((l) => ({
        type: l.type as "context" | "insert" | "delete",
        oldNumber: l.oldNumber,
        newNumber: l.newNumber,
        content: l.content,
      })),
    })),
  }));
}

export function mergeDiffFiles(files: DiffFile[]): DiffFile[] {
  const merged = new Map<string, DiffFile>();
  for (const file of files) {
    const key = file.path || file.name;
    if (!merged.has(key)) {
      merged.set(key, { ...file, diff: file.diff || "" });
      continue;
    }
    const existing = merged.get(key);
    if (!existing) continue;
    existing.additions += file.additions;
    existing.deletions += file.deletions;
    const nextDiff = file.diff || "";
    if (nextDiff) {
      existing.diff = existing.diff ? `${existing.diff}\n${nextDiff}` : nextDiff;
    }
  }
  return Array.from(merged.values());
}
