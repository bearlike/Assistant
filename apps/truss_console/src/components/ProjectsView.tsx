import { useState } from 'react';
import { Plus, FolderOpen, Loader2 } from 'lucide-react';
import { useVirtualProjects } from '../hooks/useVirtualProjects';
import { ProjectCard } from './ProjectCard';
import { NewProjectForm } from './NewProjectForm';
import { Button } from './ui/button';

export function ProjectsView() {
  const { projects, loading, error, create, update, remove } = useVirtualProjects();
  const [showForm, setShowForm] = useState(false);

  const handleCreate = async ({ name, description, path }: { name: string; description?: string; path?: string }) => {
    await create(name, description ?? '', path);
    setShowForm(false);
  };

  return (
    <div className="flex flex-col gap-6 p-6 max-w-4xl mx-auto w-full">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-[hsl(var(--foreground))]">Projects</h1>
          <p className="text-xs text-[hsl(var(--muted-foreground))] mt-0.5">
            Virtual workspaces shared across sessions
          </p>
        </div>
        {!showForm && (
          <Button size="sm" onClick={() => setShowForm(true)}>
            <Plus className="w-3.5 h-3.5" />
            New Project
          </Button>
        )}
      </div>

      {showForm && (
        <NewProjectForm
          onSubmit={handleCreate}
          onCancel={() => setShowForm(false)}
        />
      )}

      {loading ? (
        <div className="flex items-center justify-center py-12">
          <Loader2 className="w-5 h-5 animate-spin text-[hsl(var(--muted-foreground))]" />
        </div>
      ) : error ? (
        <div className="text-sm text-red-400 py-4">{error}</div>
      ) : projects.length === 0 && !showForm ? (
        <div className="flex flex-col items-center justify-center py-16 gap-3 text-[hsl(var(--muted-foreground))]">
          <FolderOpen className="w-10 h-10 opacity-40" />
          <p className="text-sm">No projects yet</p>
          <Button size="sm" variant="ghost" onClick={() => setShowForm(true)}>
            <Plus className="w-3.5 h-3.5" />
            Create your first project
          </Button>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {projects.map((project) => (
            <ProjectCard
              key={project.project_id}
              project={project}
              onEdit={update}
              onDelete={remove}
            />
          ))}
        </div>
      )}
    </div>
  );
}
