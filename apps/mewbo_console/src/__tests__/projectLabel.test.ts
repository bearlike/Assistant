import { describe, it, expect } from 'vitest';
import { ProjectLabel } from '../utils/projectLabel';
import { ProjectSummary } from '../api/contracts';

const parent: ProjectSummary = { name: 'Acme', path: '/p', project_id: 'p1', source: 'managed' };
const worktree: ProjectSummary = {
  name: 'feat-slug',
  path: '/wt',
  project_id: 'wt1',
  source: 'managed',
  is_worktree: true,
  parent_project_id: 'p1',
  branch: 'feature/login',
};

describe('ProjectLabel', () => {
  const resolver = new ProjectLabel([parent, worktree]);

  it('passes through a plain (config) project name', () => {
    expect(resolver.resolve({ project: 'Assistant' })).toEqual({ label: 'Assistant', branch: null });
  });

  it('resolves a managed project UUID to its name', () => {
    expect(resolver.resolve({ project: 'managed:p1' })).toEqual({ label: 'Acme', branch: null });
  });

  it('resolves a worktree to its parent repo name plus branch', () => {
    expect(resolver.resolve({ project: 'managed:wt1' })).toEqual({
      label: 'Acme',
      branch: 'feature/login',
    });
  });

  it('falls back to repo (then a generic label) for an unknown managed id', () => {
    expect(resolver.resolve({ project: 'managed:gone', repo: 'cached-repo' }).label).toBe('cached-repo');
    expect(resolver.resolve({ project: 'managed:gone' }).label).toBe('Managed project');
  });

  it('uses repo when no project is set', () => {
    expect(resolver.resolve({ repo: 'bare-repo' })).toEqual({ label: 'bare-repo', branch: null });
  });
});
