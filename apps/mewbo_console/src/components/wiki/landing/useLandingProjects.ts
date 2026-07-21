/**
 * Landing-page search/filter derivations: which projects, in-flight jobs,
 * and recoverable jobs are visible for the current search query — and the
 * cross-cutting `activeSlugs` set that suppresses a stale `Project` tile
 * while a fresh re-index for the same slug is in flight (the in-flight tile
 * takes precedence; once the job finalises, the project tile reappears with
 * the new `Project` record).
 */
import { useMemo } from "react";

import type { IndexingJob, Project, RecoverableJob } from "../api/types";

export function useLandingProjects(
  search: string,
  projects: Project[] | undefined,
  activeJobs: IndexingJob[] | undefined,
  recoverableJobs: RecoverableJob[] | undefined,
) {
  const activeSlugs = useMemo(
    () => new Set((activeJobs ?? []).map((j) => j.slug)),
    [activeJobs],
  );

  // Search matches against the full canonical slug (host/owner/repo) so
  // users can find their repo by org, repo, or host fragment — naturally
  // covering self-hosted enterprise instances like "git.example.io".
  const visible = useMemo(() => {
    const all = projects ?? [];
    const filteredByActive = all.filter((p) => !activeSlugs.has(p.slug));
    const q = search.trim().toLowerCase();
    if (!q) return filteredByActive;
    return filteredByActive.filter((p) => p.slug.toLowerCase().includes(q));
  }, [projects, search, activeSlugs]);

  const visibleActive = useMemo(() => {
    const all = activeJobs ?? [];
    const q = search.trim().toLowerCase();
    if (!q) return all;
    return all.filter((j) => j.slug.toLowerCase().includes(q));
  }, [activeJobs, search]);

  // Recoverable (failed / interrupted / cancelled-but-incomplete) jobs, minus
  // any that are already re-indexing — the active tile takes precedence.
  const visibleRecoverable = useMemo(() => {
    const all = (recoverableJobs ?? []).filter((j) => !activeSlugs.has(j.slug));
    const q = search.trim().toLowerCase();
    if (!q) return all;
    return all.filter((j) => j.slug.toLowerCase().includes(q));
  }, [recoverableJobs, search, activeSlugs]);

  return { visible, visibleActive, visibleRecoverable };
}
