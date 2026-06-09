import { ProjectSummary } from '../api/contracts';
import { SessionContext } from '../types';

const MANAGED_PREFIX = 'managed:';

export type ResolvedProject = {
  /** Human-readable project/repo name, or null when nothing is known. */
  label: string | null;
  /** Associated git branch, when the session runs on a worktree. */
  branch: string | null;
};

/**
 * Resolves a session's stored project identifier to a display label.
 *
 * The console persists ``context.project`` as ``managed:<uuid>`` for managed
 * projects (and worktrees), so a session card would otherwise show a raw UUID.
 * This resolver maps that id back to the project name via the live
 * ``useProjects()`` list — for a worktree it surfaces the parent repo name plus
 * the branch, giving worktree sessions the completeness they lacked. Built once
 * per render from the projects list (DI) and reused by every session row.
 */
export class ProjectLabel {
  private readonly byId: Map<string, ProjectSummary>;

  constructor(projects: ProjectSummary[]) {
    this.byId = new Map(
      projects.filter((p) => p.project_id).map((p) => [p.project_id as string, p]),
    );
  }

  resolve(context?: SessionContext): ResolvedProject {
    const raw = context?.project ?? null;
    const branch = context?.branch ?? null;
    if (!raw) return { label: context?.repo ?? null, branch };
    if (!raw.startsWith(MANAGED_PREFIX)) return { label: raw, branch };

    const project = this.byId.get(raw.slice(MANAGED_PREFIX.length));
    if (!project) return { label: context?.repo ?? 'Managed project', branch };
    if (project.is_worktree) {
      const parent = project.parent_project_id
        ? this.byId.get(project.parent_project_id)
        : undefined;
      return {
        label: parent?.name ?? context?.repo ?? project.name,
        branch: branch ?? project.branch ?? null,
      };
    }
    return { label: project.name, branch };
  }
}
