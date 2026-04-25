import { useState } from 'react';
import { FolderOpen, Pencil, Trash2, Check, X } from 'lucide-react';
import { VirtualProject } from '../types';
import { Button } from './ui/button';

interface ProjectCardProps {
  project: VirtualProject;
  onEdit: (id: string, data: { name?: string; description?: string }) => Promise<unknown>;
  onDelete: (id: string) => Promise<void>;
}

export function ProjectCard({ project, onEdit, onDelete }: ProjectCardProps) {
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(project.name);
  const [editDesc, setEditDesc] = useState(project.description);
  const [busy, setBusy] = useState(false);

  const handleSave = async () => {
    setBusy(true);
    try {
      await onEdit(project.project_id, { name: editName, description: editDesc });
      setEditing(false);
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!confirm(`Delete project "${project.name}"? This cannot be undone.`)) return;
    setBusy(true);
    try {
      await onDelete(project.project_id);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-4 flex flex-col gap-3">
      {editing ? (
        <div className="flex flex-col gap-2">
          <input
            className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-sm text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--primary))]"
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            placeholder="Project name"
          />
          <textarea
            className="w-full rounded border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-2 py-1 text-sm text-[hsl(var(--foreground))] focus:outline-none focus:ring-1 focus:ring-[hsl(var(--primary))] resize-none"
            rows={2}
            value={editDesc}
            onChange={(e) => setEditDesc(e.target.value)}
            placeholder="Description (optional)"
          />
          <div className="flex gap-2">
            <Button size="sm" onClick={handleSave} disabled={busy || !editName.trim()}>
              <Check className="w-3.5 h-3.5" />
              Save
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setEditing(false)} disabled={busy}>
              <X className="w-3.5 h-3.5" />
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-semibold text-[hsl(var(--foreground))] truncate">{project.name}</h3>
              {project.description && (
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5 line-clamp-2">{project.description}</p>
              )}
            </div>
            <div className="flex gap-1 shrink-0">
              <Button size="sm" variant="ghost" iconOnly onClick={() => setEditing(true)} aria-label="Edit project">
                <Pencil className="w-3.5 h-3.5" />
              </Button>
              <Button size="sm" variant="ghost" iconOnly onClick={handleDelete} disabled={busy} aria-label="Delete project">
                <Trash2 className="w-3.5 h-3.5 text-red-400" />
              </Button>
            </div>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
            <FolderOpen className="w-3.5 h-3.5 shrink-0" />
            <span className="truncate font-mono">{project.path}</span>
          </div>
          <div className="text-[10px] text-[hsl(var(--muted-foreground))]">
            Created {new Date(project.created_at).toLocaleDateString()}
          </div>
        </>
      )}
    </div>
  );
}
