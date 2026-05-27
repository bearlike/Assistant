/**
 * JsonValueEditor — a compact JSON textarea over an arbitrary value.
 *
 * Used by `KeyedCollectionField` to edit dict entries whose value schema is an
 * object / freeform blob (e.g. `channels`, `lsp.servers`). Mirrors the robust
 * pattern of `RecordListField`'s headers editor: a local text draft is parsed
 * on every change; valid JSON propagates via `onChange`, invalid JSON keeps the
 * draft, surfaces an inline `role="alert"` error, and is NOT propagated (so the
 * form never holds malformed data). The draft re-seeds from `value` when it
 * changes externally and we're not mid-invalid-edit.
 */
import { useEffect, useState } from "react";

import { inputBase } from "../styles";

interface JsonValueEditorProps {
  value: unknown;
  onChange: (v: unknown) => void;
  disabled?: boolean;
  id?: string;
}

export function JsonValueEditor({
  value,
  onChange,
  disabled,
  id,
}: JsonValueEditorProps) {
  const serialized = JSON.stringify(value ?? {}, null, 2);
  // Local raw text + parse error; seed from the committed value.
  const [draft, setDraft] = useState<string>(serialized);
  const [error, setError] = useState<string | null>(null);

  // Re-seed when the committed value changes from outside and we're not mid-edit
  // on an invalid buffer (which we must preserve so the user can keep typing).
  useEffect(() => {
    if (error === null) setDraft(serialized);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serialized]);

  const errorId = id ? `${id}_error` : undefined;

  const handle = (text: string) => {
    setDraft(text);
    try {
      const parsed = text.trim() === "" ? {} : JSON.parse(text);
      setError(null);
      onChange(parsed);
    } catch {
      setError("Invalid JSON.");
    }
  };

  return (
    <div className="space-y-1">
      <textarea
        id={id}
        className={`${inputBase} font-mono`}
        rows={4}
        value={draft}
        disabled={disabled}
        aria-invalid={error ? true : undefined}
        aria-describedby={error ? errorId : undefined}
        onChange={(e) => handle(e.target.value)}
      />
      {error && (
        <p
          id={errorId}
          role="alert"
          className="text-[hsl(var(--destructive))] text-xs"
        >
          {error}
        </p>
      )}
    </div>
  );
}
