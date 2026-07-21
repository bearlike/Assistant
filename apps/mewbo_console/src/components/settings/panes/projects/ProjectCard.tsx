/**
 * ProjectCard — one managed project row inside the Workspace facet's
 * `ProjectsPane` card (it is no longer a page-level card of its own).
 *
 * Deleting a managed project is destructive and irreversible — Mewbo owns the
 * directory — so it is gated by the shared `<ConfirmDialog>` (`ui/confirm-dialog`),
 * the same primitive `ApiKeysView`, `GitCredentialsView`, `WorktreesPanel` and
 * `TriggersPane` confirm with. The old `window.confirm()` is gone.
 */
import { useState } from 'react';
import { FolderOpen, Pencil, Trash2, Check, X } from 'lucide-react';

import { VirtualProject } from '../../../../types';
import { Button } from '../../../ui/button';
import { Input } from '../../../ui/input';
import { Textarea } from '../../../ui/textarea';
import { ConfirmDialog } from '../../../ui/confirm-dialog';
import { WorktreesPanel } from './WorktreesPanel';

interface ProjectCardProps {
  project: VirtualProject;
  onEdit: (id: string, data: { name?: string; description?: string }) => Promise<unknown>;
  onDelete: (id: string) => Promise<void>;
  /** `true` while a delete mutation is in flight (owned by the pane's hook). */
  deleting?: boolean;
}

export function ProjectCard({ project, onEdit, onDelete, deleting = false }: ProjectCardProps) {
  const [editing, setEditing] = useState(false);
  const [editName, setEditName] = useState(project.name);
  const [editDesc, setEditDesc] = useState(project.description);
  const [saving, setSaving] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<VirtualProject | null>(null);

  const handleSave = async () => {
    setSaving(true);
    try {
      await onEdit(project.project_id, { name: editName, description: editDesc });
      setEditing(false);
    } finally {
      setSaving(false);
    }
  };

  // The dialog must close whether the delete lands or throws: an unguarded
  // `await` left it open forever on a rejected mutation, with no error shown.
  // The failure itself is the hook layer's job to surface (toast); this handler
  // only guarantees the confirm can't wedge.
  const handleDeleteConfirm = async () => {
    try {
      await onDelete(project.project_id);
    } catch {
      /* surfaced by the mutation's own error handling */
    } finally {
      setDeleteTarget(null);
    }
  };

  return (
    <div className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-4 py-3 flex flex-col gap-3">
      {editing ? (
        <div className="flex flex-col gap-2">
          <Input
            value={editName}
            onChange={(e) => setEditName(e.target.value)}
            placeholder="Project name"
            aria-label="Project name"
          />
          <Textarea
            rows={2}
            value={editDesc}
            onChange={(e) => setEditDesc(e.target.value)}
            placeholder="Description (optional)"
            aria-label="Project description"
            className="resize-none"
          />
          <div className="flex gap-2">
            <Button
              size="sm"
              variant="primary"
              onClick={handleSave}
              disabled={saving || !editName.trim()}
              leadingIcon={<Check className="w-3.5 h-3.5" />}
            >
              Save
            </Button>
            <Button
              size="sm"
              variant="ghost"
              onClick={() => setEditing(false)}
              disabled={saving}
              leadingIcon={<X className="w-3.5 h-3.5" />}
            >
              Cancel
            </Button>
          </div>
        </div>
      ) : (
        <>
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0 flex-1">
              <h3 className="text-sm font-medium text-[hsl(var(--foreground))] truncate">
                {project.name}
              </h3>
              {project.description && (
                <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5 line-clamp-2">
                  {project.description}
                </p>
              )}
            </div>
            <div className="flex gap-1 shrink-0">
              <Button
                size="sm"
                variant="ghost"
                iconOnly
                onClick={() => setEditing(true)}
                aria-label={`Edit project ${project.name}`}
                title="Edit name and description"
                leadingIcon={<Pencil className="w-3.5 h-3.5" />}
              />
              <Button
                size="sm"
                variant="ghost"
                tone="danger"
                iconOnly
                onClick={() => setDeleteTarget(project)}
                disabled={deleting}
                aria-label={`Delete project ${project.name}`}
                title="Delete project and its worktrees"
                leadingIcon={<Trash2 className="w-3.5 h-3.5" />}
              />
            </div>
          </div>
          <div className="flex items-center gap-1.5 text-xs text-[hsl(var(--muted-foreground))]">
            <FolderOpen className="w-3.5 h-3.5 shrink-0" />
            <span className="truncate font-mono">{project.path}</span>
          </div>
          {/* Worktrees: only relevant for top-level managed projects. */}
          {!project.is_worktree && <WorktreesPanel projectId={project.project_id} />}
        </>
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete project?"
        description={
          <>
            This permanently deletes the managed project{' '}
            <span className="font-medium text-[hsl(var(--foreground))]">{deleteTarget?.name}</span>
            {' '}and any worktrees under it. Sessions already scoped to it lose their workspace.
            This cannot be undone.
          </>
        }
        confirmLabel="Delete project"
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="w-4 h-4" />}
        pending={deleting}
        onConfirm={() => void handleDeleteConfirm()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
