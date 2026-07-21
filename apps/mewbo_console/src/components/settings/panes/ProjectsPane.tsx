/**
 * ProjectsPane — managed ("virtual") projects, a pane of the Workspace facet.
 *
 * Replaces the standalone `/projects` page: zero props, its own TanStack Query
 * data (`useVirtualProjects`, sharing the `['projects']` cache with
 * `useProjects()`), bare `<SettingsCard>`s, no page chrome — the Settings shell
 * owns width/padding and the facet heading.
 *
 * ## Two systems, one facet — say it out loud
 * The Workspace facet renders this pane ABOVE the schema-driven `projects`
 * config section, and users conflate them constantly. They are not the same:
 *   - config `projects` (a dict in `app.json`) — directories YOU already have,
 *     registered by hand under a name. Mewbo only points sessions at them and
 *     re-creates a missing folder on demand (promote-on-demand).
 *   - managed projects (this pane, `/api/projects`) — workspaces MEWBO creates
 *     and owns, including the git worktrees it opens for parallel sessions.
 *     Server-owned means server-deleted: the reaper permanently removes a
 *     provided-path parent once it has no worktree children left.
 * The card description below carries that distinction (one line + `?` popover
 * via `FieldHelp`); don't drop it.
 */
import { useState } from 'react';
import { FolderOpen, Loader2, Plus } from 'lucide-react';

import { useVirtualProjects } from '../../../hooks/useVirtualProjects';
import { Button } from '../../ui/button';
import { ErrorAlert } from '../../ErrorAlert';
import { SettingsCard } from '../SettingsCard';
import { ProjectCard } from './projects/ProjectCard';
import { NewProjectForm, type NewProjectFormValues } from './projects/NewProjectForm';

const MANAGED_PROJECTS_HELP = [
  'Workspaces Mewbo creates and owns on disk, including the git worktrees it opens for parallel sessions.',
  '',
  'This facet holds two different things. These managed projects are **server-owned**: Mewbo made the directory, and deleting one here removes the project and its worktrees for good. It also reaps a managed parent on its own once the last worktree under it is gone.',
  '',
  'The **Projects** map below is the other one. Those are directories **you** already have, registered by name in `app.json`. Mewbo only points sessions at them; it never deletes them.',
].join('\n');

export function ProjectsPane() {
  const { projects, loading, error, create, update, remove, removing } = useVirtualProjects();
  const [showForm, setShowForm] = useState(false);

  const handleCreate = async ({ name, description, path }: NewProjectFormValues) => {
    await create(name, description ?? '', path);
    setShowForm(false);
  };

  return (
    <div className="space-y-4">
      <SettingsCard
        id="settings-managed-projects"
        title="Managed projects"
        description={MANAGED_PROJECTS_HELP}
        actions={
          !showForm && (
            <Button
              variant="primary"
              size="md"
              onClick={() => setShowForm(true)}
              leadingIcon={<Plus className="w-4 h-4" />}
            >
              New project
            </Button>
          )
        }
      >
        {showForm && (
          <NewProjectForm onSubmit={handleCreate} onCancel={() => setShowForm(false)} />
        )}

        {error && <ErrorAlert error={error} fallback="Failed to load managed projects" />}

        {loading && (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
          </div>
        )}

        {!loading && !error && projects.length === 0 && !showForm && (
          <div className="rounded-lg border border-dashed border-[hsl(var(--border))] px-4 py-6 text-center">
            <FolderOpen className="w-6 h-6 mx-auto text-[hsl(var(--muted-foreground))] opacity-40" />
            <p className="mt-2 text-sm text-[hsl(var(--muted-foreground))]">
              No managed projects yet.
            </p>
            <p className="mt-1 text-xs text-[hsl(var(--muted-foreground))]">
              Create one to give sessions a workspace Mewbo owns end to end. It can branch a
              worktree per session and clean up after itself. To point at a directory you already
              keep, use the Projects map below instead.
            </p>
          </div>
        )}

        {!loading && projects.length > 0 && (
          <div className="space-y-2">
            {projects.map((project) => (
              <ProjectCard
                key={project.project_id}
                project={project}
                onEdit={update}
                onDelete={remove}
                deleting={removing}
              />
            ))}
          </div>
        )}
      </SettingsCard>
    </div>
  );
}
