/**
 * CatalogDocsForm — document input for non-git catalog workspaces.
 *
 * Two input paths:
 *   1. Paste — one editable card per document (title + body textarea).
 *      "Add document" appends another card. Cards can be removed.
 *   2. Upload — <input type="file" multiple> that accepts .txt/.md files.
 *      File name → title, file contents → text.
 *
 * Emits `onChange(docs)` whenever the list changes. Parent controls the
 * authoritative copy; this component renders it.
 */

import { useRef } from "react";
import { FileText, Plus, Trash2, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { CatalogDocument } from "./api/types";

/** Slugify a string for use as a doc id. */
function slugifyDocId(text: string, index: number): string {
  const base = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
  return base || `doc-${index + 1}`;
}

interface CatalogDocsFormProps {
  docs: CatalogDocument[];
  onChange: (docs: CatalogDocument[]) => void;
  /** Error message shown below the section. */
  error?: string;
}

export function CatalogDocsForm({ docs, onChange, error }: CatalogDocsFormProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);

  // ── Paste path ─────────────────────────────────────────────────────

  const addDoc = () => {
    const idx = docs.length;
    onChange([
      ...docs,
      { id: `doc-${idx + 1}`, title: "", text: "" },
    ]);
  };

  const removeDoc = (idx: number) => {
    onChange(docs.filter((_, i) => i !== idx));
  };

  const updateDoc = (idx: number, patch: Partial<CatalogDocument>) => {
    const next = docs.map((d, i) => {
      if (i !== idx) return d;
      const merged = { ...d, ...patch };
      // Recompute id from title whenever title changes (unless text-only update).
      if ("title" in patch) {
        merged.id = slugifyDocId(merged.title, idx);
      }
      return merged;
    });
    onChange(next);
  };

  // ── Upload path ─────────────────────────────────────────────────────

  const handleFiles = (files: FileList | null) => {
    if (!files || files.length === 0) return;
    const readers: Promise<CatalogDocument>[] = [];
    for (let i = 0; i < files.length; i++) {
      const file = files[i];
      const title = file.name.replace(/\.(txt|md)$/i, "");
      readers.push(
        file.text().then((text) => ({
          id: slugifyDocId(title, docs.length + i),
          title,
          text,
        })),
      );
    }
    Promise.all(readers).then((loaded) => {
      onChange([...docs, ...loaded]);
    });
  };

  return (
    <div className="space-y-3">
      {/* Document cards */}
      {docs.length > 0 && (
        <div className="space-y-3">
          {docs.map((doc, idx) => (
            <DocCard
              key={idx}
              idx={idx}
              doc={doc}
              onUpdate={(patch) => updateDoc(idx, patch)}
              onRemove={() => removeDoc(idx)}
            />
          ))}
        </div>
      )}

      {/* Empty state */}
      {docs.length === 0 && (
        <div className="flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-[hsl(var(--border))] bg-[hsl(var(--muted))]/20 py-8 px-4 text-center">
          <FileText className="h-6 w-6 text-[hsl(var(--muted-foreground))]" />
          <p className="text-sm text-[hsl(var(--muted-foreground))]">
            No documents yet — add one below or upload files.
          </p>
        </div>
      )}

      {/* Error */}
      {error && (
        <p className="text-xs text-[hsl(var(--destructive))]">{error}</p>
      )}

      {/* Action row */}
      <div className="flex items-center gap-2 flex-wrap">
        <Button
          type="button"
          variant="neutral"
          size="sm"
          onClick={addDoc}
          leadingIcon={<Plus className="h-3.5 w-3.5" />}
        >
          Add document
        </Button>
        <Button
          type="button"
          variant="neutral"
          size="sm"
          onClick={() => fileInputRef.current?.click()}
          leadingIcon={<Upload className="h-3.5 w-3.5" />}
        >
          Upload files
        </Button>
        <span className="text-[11px] text-[hsl(var(--muted-foreground))]">
          Accepts .txt and .md files
        </span>
        {/* Hidden file input */}
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".txt,.md,text/plain,text/markdown"
          className="sr-only"
          aria-hidden
          onChange={(e) => handleFiles(e.target.files)}
        />
      </div>
    </div>
  );
}

// ── DocCard ─────────────────────────────────────────────────────────────────

function DocCard({
  idx,
  doc,
  onUpdate,
  onRemove,
}: {
  idx: number;
  doc: CatalogDocument;
  onUpdate: (patch: Partial<CatalogDocument>) => void;
  onRemove: () => void;
}) {
  return (
    <div
      className={cn(
        "rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] overflow-hidden",
      )}
    >
      {/* Card header row */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-[hsl(var(--border))] bg-[hsl(var(--muted))]/20">
        <FileText className="h-3.5 w-3.5 text-[hsl(var(--muted-foreground))] shrink-0" />
        <input
          type="text"
          placeholder={`Document ${idx + 1} title`}
          value={doc.title}
          onChange={(e) => onUpdate({ title: e.target.value })}
          aria-label={`Title for document ${idx + 1}`}
          className="flex-1 bg-transparent text-sm outline-none placeholder:text-[hsl(var(--muted-foreground))]"
        />
        <button
          type="button"
          aria-label={`Remove document ${idx + 1}`}
          title="Remove"
          onClick={onRemove}
          className="inline-flex items-center justify-center w-6 h-6 rounded text-[hsl(var(--muted-foreground))] hover:text-[hsl(var(--destructive))] hover:bg-[hsl(var(--destructive))]/10 transition-colors"
        >
          <Trash2 className="h-3.5 w-3.5" />
        </button>
      </div>

      {/* Body */}
      <textarea
        rows={6}
        spellCheck={false}
        placeholder="Paste or type document content…"
        value={doc.text}
        onChange={(e) => onUpdate({ text: e.target.value })}
        aria-label={`Content for document ${idx + 1}`}
        className="w-full bg-transparent font-mono text-xs text-[hsl(var(--foreground))] px-3 py-2.5 outline-none resize-none placeholder:text-[hsl(var(--muted-foreground))]"
      />
    </div>
  );
}
