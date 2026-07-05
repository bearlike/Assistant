package com.mewbo.aura.data.repo

import com.mewbo.aura.data.api.AuraApi
import com.mewbo.aura.data.model.ProjectSummary
import com.mewbo.aura.data.model.ToolSummary
import javax.inject.Inject
import kotlinx.coroutines.CancellationException

/** The composer options sheet's two catalogs (`GET api/projects`, `GET api/tools`) - plain suspend
 * fetches, no caching (task brief: the sheet re-fetches on every open, same as [ModelRepository]'s
 * `null`-on-failure degrade but without the cache). */
class SessionScopeRepository @Inject constructor(
    private val api: AuraApi,
) {
    // Gitea #181 fix wave, finding 2: both fetches run inside ChatViewModel's own async{} pairs
    // (refreshComposerScope/bind's prefetch/selectProject) - a cancelled coroutine there must
    // propagate, not resolve to null and let the caller act on a "failure" that was really a
    // cancellation. rethrowCancellation before falling back to the degrade-to-null path.
    suspend fun projects(): List<ProjectSummary>? =
        runCatching { api.getProjects().projects.map { it.toDomain() } }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()

    /** MCP tools, plus capability-gated product tools (wiki_*, scg_*, agentic_search - Gitea #182
     * P1: `server`-grouped entries at `scope == "plugin"`). Plain core builtins (shell, edit, ...)
     * stay excluded - they have no per-run allowlist knob and would flood the picker with entries
     * nobody can usefully toggle. The backend's own `classify_tool_scope` always resolves a
     * non-MCP core builtin to `scope == "builtin"`, so `scope == "plugin"` cleanly picks out ONLY
     * the new product rows without a second `kind` check (a plugin-sourced MCP server also happens
     * to read `scope == "plugin"`, but it's already included via `kind == "mcp"` - no double-count
     * risk, this is a plain OR). */
    suspend fun tools(project: String? = null): List<ToolSummary>? =
        runCatching {
            api.getTools(project).tools
                .filter { it.kind == "mcp" || it.scope == "plugin" }
                .map { it.toDomain() }
        }
            .onFailure { if (it is CancellationException) throw it }
            .getOrNull()
}
