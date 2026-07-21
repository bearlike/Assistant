import { FileText, ImageIcon } from 'lucide-react';
import { AttachmentPayload } from '../types';
import { formatBytes } from '../lib/utils';

/** Narrow the `AttachmentMeta | AttachmentRecord` union to a display name. */
function attachmentName(a: AttachmentPayload): string {
  return 'filename' in a ? a.filename : a.name;
}

/** Narrow the union to a byte size. */
function attachmentSize(a: AttachmentPayload): number {
  return 'size_bytes' in a ? a.size_bytes : a.size;
}

/** Narrow the union to a MIME type string. */
function attachmentType(a: AttachmentPayload): string {
  return 'content_type' in a ? a.content_type : a.type;
}

interface AttachmentCardsProps {
  attachments: AttachmentPayload[];
}

/**
 * Glanceable, metadata-only attachment tiles rendered above a user query
 * bubble — one tile per uploaded file, right-aligned and wrapping for
 * multiples. Shows a type glyph (image vs. document), the filename
 * (ellipsized to ~2 lines), and a humanized size. No thumbnails/pixels —
 * this is a manifest of what was attached, not a preview.
 */
export function AttachmentCards({ attachments }: AttachmentCardsProps) {
  if (!attachments || attachments.length === 0) return null;
  return (
    <div className="flex flex-wrap justify-end gap-1.5 mb-1.5">
      {attachments.map((att, i) => {
        const name = attachmentName(att);
        const isImage = attachmentType(att).toLowerCase().startsWith('image/');
        const Icon = isImage ? ImageIcon : FileText;
        return (
          <div
            key={`${name}-${i}`}
            title={name}
            className="flex items-center gap-1.5 max-w-[160px] rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--card))] px-2 py-1.5"
          >
            <Icon
              className="w-3.5 h-3.5 shrink-0 text-[hsl(var(--muted-foreground))]"
              aria-hidden
            />
            <div className="min-w-0 flex flex-col items-start">
              <span className="w-full text-2xs font-medium text-[hsl(var(--foreground))] leading-snug line-clamp-2 break-all text-left">
                {name}
              </span>
              <span className="text-2xs text-[hsl(var(--muted-foreground))]">
                {formatBytes(attachmentSize(att))}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}
