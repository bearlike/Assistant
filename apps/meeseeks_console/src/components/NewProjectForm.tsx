import { useState } from 'react';
import { Button } from './ui/Button';

interface NewProjectFormProps {
  onSubmit: (name: string, description: string, path?: string) => Promise<void>;
  onCancel: () => void;
}

export function NewProjectForm({ onSubmit, onCancel }: NewProjectFormProps) {
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSubmit(name.trim(), description.trim(), path.trim() || undefined);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create project');
    } finally {
      setBusy(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 flex flex-col gap-3"
    >
      <h3 className="text-sm font-semibold text-[hsl(var(--foreground))]">New Project</h3>
      <div className="flex flex-col gap-1">
        <label className="text-xs text-[hsl(var(--muted-foreground))]">Name *</label>
        <input
          className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1.5 text-sm text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--primary))]"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="My Project"
          required
          autoFocus
        />
      </div>
      <div className="flex flex-col gap-1">
        <label className="text-xs text-[hsl(var(--muted-foreground))]">Description</label>
        <textarea
          className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1.5 text-sm text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--primary))] resize-none"
          rows={2}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder="What is this project about?"
        />
      </div>
      <div className="flex flex-col gap-1">
        <label className="text-xs text-[hsl(var(--muted-foreground))]">Path (optional — auto-generated if empty)</label>
        <input
          className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1.5 text-sm text-[hsl(var(--foreground))] font-mono focus:outline-none focus:ring-1 focus:ring-[hsl(var(--primary))]"
          value={path}
          onChange={(e) => setPath(e.target.value)}
          placeholder="/path/to/workspace"
        />
      </div>
      {error && (
        <p className="text-xs text-red-400">{error}</p>
      )}
      <div className="flex gap-2">
        <Button type="submit" size="sm" disabled={busy || !name.trim()}>
          {busy ? 'Creating…' : 'Create Project'}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
