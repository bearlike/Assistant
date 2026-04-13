import { useEffect, useRef, useState } from 'react';
import { Loader2, Pencil, Sparkles } from 'lucide-react';
import { Button } from './ui/Button';

/**
 * Inline-editable title with optional AI regeneration.
 *
 * - View mode: renders the title with a pencil icon that appears on hover of
 *   the enclosing `group` parent. Click the pencil or double-click the title
 *   to enter edit mode.
 * - Edit mode: renders a controlled `<input>`. Enter saves, Escape cancels,
 *   blur saves. Empty/whitespace values are treated as cancel.
 *   When `onRegenerate` is provided, a sparkle button triggers AI title
 *   regeneration and populates the input with the result.
 *
 * Parent is responsible for wrapping in a `group` element if hover-reveal
 * is desired (NavBar wraps the title block in `group`).
 */
export function EditableTitle({
  value,
  onSave,
  onRegenerate,
  className = '',
  maxLength = 120
}: {
  value: string;
  onSave: (next: string) => Promise<void>;
  onRegenerate?: () => Promise<string>;
  className?: string;
  maxLength?: number;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const [regenerating, setRegenerating] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);

  useEffect(() => {
    if (editing && inputRef.current) {
      inputRef.current.focus();
      inputRef.current.select();
    }
  }, [editing]);

  const startEdit = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (saving) return;
    setDraft(value);
    setEditing(true);
  };

  const commit = async () => {
    const next = draft.trim().slice(0, maxLength);
    if (!next || next === value.trim()) {
      setEditing(false);
      setDraft(value);
      return;
    }
    setSaving(true);
    try {
      await onSave(next);
      setEditing(false);
    } catch {
      // Revert on failure, keep edit mode focused for retry.
      setDraft(value);
      inputRef.current?.focus();
      inputRef.current?.select();
    } finally {
      setSaving(false);
    }
  };

  const cancel = () => {
    setDraft(value);
    setEditing(false);
  };

  const handleRegenerate = async () => {
    if (!onRegenerate || regenerating) return;
    setRegenerating(true);
    try {
      const newTitle = await onRegenerate();
      setDraft(newTitle);
    } catch {
      // Keep current draft on failure
    } finally {
      setRegenerating(false);
    }
  };

  if (editing) {
    return (
      <div className="flex items-center gap-1 min-w-0">
        <input
          ref={inputRef}
          value={draft}
          maxLength={maxLength}
          disabled={saving || regenerating}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void commit();
            } else if (e.key === 'Escape') {
              e.preventDefault();
              cancel();
            }
          }}
          onBlur={() => {
            if (!saving && !regenerating) void commit();
          }}
          onClick={(e) => e.stopPropagation()}
          aria-label="Edit session title"
          className={`${className} bg-transparent border-b border-[hsl(var(--border))] focus:border-[hsl(var(--ring))] outline-none flex-1 min-w-0 px-0.5 disabled:opacity-60`}
        />
        {onRegenerate && (
          <Button
            variant="ghost"
            size="sm"
            iconOnly
            onClick={(e) => { e.stopPropagation(); void handleRegenerate(); }}
            onMouseDown={(e) => e.preventDefault()}
            disabled={regenerating}
            aria-label="Regenerate title with AI"
            className="shrink-0">
            {regenerating
              ? <Loader2 className="w-3 h-3 animate-spin" />
              : <Sparkles className="w-3 h-3" />}
          </Button>
        )}
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5 min-w-0">
      <h2
        className={`${className} truncate cursor-text`}
        onDoubleClick={startEdit}
        title={value}>
        {value}
      </h2>
      <Button
        variant="ghost"
        size="sm"
        iconOnly
        onClick={startEdit}
        aria-label="Edit title"
        className="shrink-0 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity">
        <Pencil className="w-3 h-3" />
      </Button>
    </div>
  );
}
